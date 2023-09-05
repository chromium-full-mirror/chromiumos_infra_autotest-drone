# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
""" This module provides convenience routines to access Flash ROM (EEPROM)

saft_flashrom_util is based on utility 'flashrom'.

Original tool syntax:
    (read ) flashrom -r <file>
    (write) flashrom -l <layout_fn> [-i <image_name> ...] -w <file>

The layout_fn is in format of
    address_begin:address_end image_name
    which defines a region between (address_begin, address_end) and can
    be accessed by the name image_name.

Currently the tool supports multiple partial write but not partial read.

In the saft_flashrom_util, we provide read and partial write abilities.
For more information, see help(saft_flashrom_util.flashrom_util).
"""


class TestError(Exception):
    """Represents an internal error, such as invalid arguments."""
    pass


class LayoutScraper(object):
    """Object of this class is used to retrieve layout from a BIOS file."""

    DEFAULT_CHROMEOS_FMAP_CONVERSION = {
            "BOOT_STUB": "FV_BSTUB",
            "RO_FRID": "RO_FRID",
            "GBB": "FV_GBB",
            "RECOVERY": "FVDEV",
            "VBLOCK_A": "VBOOTA",
            "VBLOCK_B": "VBOOTB",
            "FW_MAIN_A": "FVMAIN",
            "FW_MAIN_B": "FVMAINB",
            "RW_FWID_A": "RW_FWID_A",
            "RW_FWID_B": "RW_FWID_B",
            # Intel CSME FW Update sections
            "ME_RW_A": "ME_RW_A",
            "ME_RW_B": "ME_RW_B",
            # Memory Training data cache for recovery boots
            # Added on Nov 09, 2016
            "RECOVERY_MRC_CACHE": "RECOVERY_MRC_CACHE",
            # New sections in Depthcharge.
            "EC_MAIN_A": "ECMAINA",
            "EC_MAIN_B": "ECMAINB",
            # EC firmware layout
            "EC_RW": "EC_RW",
            "EC_RW_B": "EC_RW_B",
            "RW_FWID": "RW_FWID",
            "RW_LEGACY": "RW_LEGACY",
    }

    def __init__(self, os_if):
        self.image = None
        self.os_if = os_if

    def check_layout(self, layout, file_size):
        """Verify the layout to be consistent.

        The layout is consistent if there is no overlapping sections and the
        section boundaries do not exceed the file size.

        Inputs:
          layout: a dictionary keyed by a string (the section name) with
                  values being two integers tuples, the first and the last
                  bites' offset in the file.
          file_size: and integer, the size of the file the layout describes
                     the sections in.

        Raises:
          TestError in case the layout is not consistent.
        """

        # Generate a list of section range tuples.
        ost = sorted([layout[section] for section in layout])
        base = -1
        for section_base, section_end in ost:
            if section_base <= base or section_end + 1 < section_base:
                # Overlapped section is possible, like the fwid which is
                # inside the main fw section.
                self.os_if.log('overlapped section at 0x%x..0x%x' %
                               (section_base, section_end))
            base = section_end
        if base > file_size:
            raise TestError('Section end 0x%x exceeds file size %x' %
                            (base, file_size))

    def get_layout(self, file_name):
        """Generate layout for a firmware file.

        Internally, this uses the "dump_fmap" command, and converts
        the output into a dictionary mapping region names to 2-tuples
        of the start and last addresses.

        Then verify the generated layout's consistency and return it to the
        caller.
        """
        command = 'dump_fmap -p %s' % file_name
        layout_data = {}  # keyed by the section name, elements - tuples of
        # (<section start addr>, <section end addr>)

        for line in self.os_if.run_shell_command_get_output(command):
            region_name, offset, size = line.split()

            try:
                name = self.DEFAULT_CHROMEOS_FMAP_CONVERSION[region_name]
            except KeyError:
                continue  # This line does not contain an area of interest.

            if name in layout_data:
                raise TestError('%s duplicated in the layout' % name)

            offset = int(offset)
            size = int(size)
            layout_data[name] = (offset, offset + size - 1)

        self.check_layout(layout_data, self.os_if.get_file_size(file_name))
        return layout_data


# flashrom utility wrapper
class flashrom_util(object):
    """ a wrapper for "flashrom" utility.

    You can read, write, or query flash ROM size with this utility.
    Although you can do "partial-write", the tools always takes a
    full ROM image as input parameter.

    NOTE before accessing flash ROM, you may need to first "select"
    your target - usually BIOS or EC. That part is not handled by
    this utility. Please find other external script to do it.

    To perform a read, you need to:
     1. Prepare a flashrom_util object
        ex: flashrom = flashrom_util.flashrom_util()
     2. Perform read operation
        ex: image = flashrom.read_whole()

        When the contents of the flashrom is read off the target, it's map
        gets created automatically (read from the flashrom image using
        'dump_fmap'). If the user wants this object to operate on some other
        file, they could either have the map for the file created explicitly by
        invoking flashrom.set_firmware_layout(filename), or supply their own map
        (which is a dictionary where keys are section names, and values are
        tuples of integers, base address of the section and the last address
        of the section).

    By default this object operates on the map retrieved from the image and
    stored locally, this map can be overwritten by an explicitly passed user
    map.

    To perform a (partial) write:

     1. Prepare a buffer storing an image to be written into the flashrom.
     2. Have the map generated automatically or prepare your own, for instance:
        ex: layout_map_all = { 'all': (0, rom_size - 1) }
        ex: layout_map = { 'ro': (0, 0xFFF), 'rw': (0x1000, rom_size-1) }
     4. Perform write operation

        ex using default map:
          flashrom.write_partial(new_image, (<section_name>, ...))
        ex using explicitly provided map:
          flashrom.write_partial(new_image, layout_map_all, ('all',))
    """

    def __init__(self, os_if, keep_temp_files=False, target_is_ec=False):
        """ constructor of flashrom_util. help(flashrom_util) for more info

        @param os_if: an object providing interface to OS services
        @param keep_temp_files: if true, preserve temp files after operations
        @param target_is_ec: if false, target is BIOS/AP

        @type os_if: client.cros.faft.utils.os_interface.OSInterface
        @type keep_temp_files: bool
        @type target_is_ec: bool
        """
        self.os_if = os_if
        self.keep_temp_files = keep_temp_files
        self.firmware_layout = {}
        self._target_is_ec = target_is_ec

    @property
    def _target_command(self):
        """
        Generates programmer name to use with flashrom commands, this should be
        deleted once flashrom commands are fully replaced with futility/ectool.
        """
        return '-p ec' if self._target_is_ec else '-p internal'

    def _get_temp_filename(self, prefix):
        """Returns name of a temporary file in /tmp."""
        return self.os_if.create_temp_file(prefix)

    def _remove_temp_file(self, filename):
        """Removes a temp file if self.keep_temp_files is false."""
        if self.keep_temp_files:
            return
        if self.os_if.path_exists(filename):
            self.os_if.remove_file(filename)

    def _create_layout_file(self, layout_map):
        """Creates a layout file based on layout_map.

        Returns the file name containing layout information.
        """
        layout_text = [
                '0x%08lX:0x%08lX %s' % (v[0], v[1], k)
                for k, v in layout_map.items()
        ]
        layout_text.sort()  # XXX unstable if range exceeds 2^32
        tmpfn = self._get_temp_filename('lay_')
        self.os_if.write_file(tmpfn, '\n'.join(layout_text) + '\n')
        return tmpfn

    def check_target(self):
        """Check if flashrom programmer is working, by specifying no commands.

        The command executed is just 'flashrom -p <target>'.

        @return: True if flashrom completed successfully
        @raise autotest_lib.client.common_lib.error.CmdError: if flashrom failed
        """
        cmd = 'flashrom %s' % self._target_command
        self.os_if.run_shell_command(cmd)
        return True

    def get_section(self, base_image, section_name):
        """
        Retrieves a section of data based on section_name in layout_map.
        Raises error if unknown section or invalid layout_map.
        """
        if section_name not in self.firmware_layout:
            return ''
        pos = self.firmware_layout[section_name]
        if pos[0] >= pos[1] or pos[1] >= len(base_image):
            raise TestError(
                    'INTERNAL ERROR: invalid layout map: %s.' % section_name)
        blob = base_image[pos[0]:pos[1] + 1]
        # Trim down the main firmware body to its actual size since the
        # signing utility uses the size of the input file as the size of
        # the data to sign. Make it the same way as firmware creation.
        if section_name in ('FVMAIN', 'FVMAINB', 'ECMAINA', 'ECMAINB'):
            align = 4
            pad = blob[-1]
            blob = blob.rstrip(pad)
            blob = blob + ((align - 1) - (len(blob) - 1) % align) * pad
        return blob

    def put_section(self, base_image, section_name, data):
        """
        Updates a section of data based on section_name in firmware_layout.
        Raises error if unknown section.
        Returns the full updated image data.
        """
        pos = self.firmware_layout[section_name]
        if pos[0] >= pos[1] or pos[1] >= len(base_image):
            raise TestError('INTERNAL ERROR: invalid layout map.')
        if len(data) != pos[1] - pos[0] + 1:
            # Pad the main firmware body since we trimed it down before.
            if (len(data) < pos[1] - pos[0] + 1
                        and section_name in ('FVMAIN', 'FVMAINB', 'ECMAINA',
                                             'ECMAINB', 'RW_FWID')):
                pad = base_image[pos[1]]
                data = data + pad * (pos[1] - pos[0] + 1 - len(data))
            else:
                raise TestError('INTERNAL ERROR: unmatched data size.')
        return base_image[0:pos[0]] + data + base_image[pos[1] + 1:]

    def get_size(self):
        """ Gets size of current flash ROM """
        # TODO(hungte) Newer version of tool (flashrom) may support --get-size
        # command which is faster in future. Right now we use back-compatible
        # method: read whole and then get length.
        image = self.read_whole()
        return len(image)

    def set_firmware_layout(self, file_name):
        """get layout read from the BIOS """

        scraper = LayoutScraper(self.os_if)
        self.firmware_layout = scraper.get_layout(file_name)

    def enable_write_protect(self):
        """Enable the write protection of the flash chip."""
        if self._target_is_ec:
            cmd = 'ectool flashprotect enable now'
        else:
            cmd = 'futility flash --wp-enable'
        self.os_if.run_shell_command(cmd, modifies_device=True)

    def disable_write_protect(self):
        """Disable the write protection of the flash chip."""
        if self._target_is_ec:
            cmd = 'ectool flashprotect disable'
        else:
            cmd = 'futility flash --wp-disable'
        self.os_if.run_shell_command(cmd, modifies_device=True)

    def get_write_protect_status(self):
        """Get a bool describing the status of the write protection.

        @return: True if WP is enabled, False if WP is disabled
        @rtype: bool
        """
        if self._target_is_ec:
            cmd = 'ectool flashprotect'
            ec_status = self.os_if.run_shell_command_get_output(cmd)
            active_flags_line = [
                    l for l in ec_status if l.startswith("Flash protect flags")
            ]
            active_flags = active_flags_line[0].split()
            return ('ro_now' in active_flags or 'all_now' in active_flags)
        else:
            cmd = 'futility flash --wp-status --ignore-hw'
            status = self.os_if.run_shell_command_get_output(cmd)
            return 'enabled' in status[0]

    def dump_flash(self, filename):
        """Read the flash device's data into a file, but don't parse it."""
        cmd = 'flashrom %s -r "%s"' % (self._target_command, filename)
        self.os_if.log('flashrom_util.dump_flash(): %s' % cmd)
        self.os_if.run_shell_command(cmd)

    def read_whole(self):
        """
        Reads whole flash ROM data.
        Returns the data read from flash ROM, or empty string for other error.
        """
        tmpfn = self._get_temp_filename('rd_')
        cmd = 'flashrom %s -r "%s"' % (self._target_command, tmpfn)
        self.os_if.log('flashrom_util.read_whole(): %s' % cmd)
        self.os_if.run_shell_command(cmd)
        result = self.os_if.read_file(tmpfn)
        self.set_firmware_layout(tmpfn)

        # clean temporary resources
        self._remove_temp_file(tmpfn)
        return result

    def write_partial(self, base_image, write_list, write_layout_map=None):
        """
        Writes data in sections of write_list to flash ROM.
        An exception is raised if write operation fails.
        """

        if write_layout_map:
            layout_map = write_layout_map
        else:
            layout_map = self.firmware_layout

        tmpfn = self._get_temp_filename('wr_')
        self.os_if.write_file(tmpfn, base_image)
        layout_fn = self._create_layout_file(layout_map)

        write_cmd = 'flashrom %s -l "%s" -i %s -w "%s"' % (
                self._target_command, layout_fn, ' -i '.join(write_list),
                tmpfn)
        self.os_if.log('flashrom.write_partial(): %s' % write_cmd)
        self.os_if.run_shell_command(write_cmd, modifies_device=True)

        # clean temporary resources
        self._remove_temp_file(tmpfn)
        self._remove_temp_file(layout_fn)

    def write_whole(self, base_image):
        """Write the whole base image. """
        layout_map = {'all': (0, len(base_image) - 1)}
        self.write_partial(base_image, ('all', ), layout_map)

    def get_write_cmd(self, image=None):
        """Get the command to write the whole image (no layout handling)

        @param image: the filename (empty to use current handler data)
        """
        return 'flashrom %s -w "%s"' % (self._target_command, image)
