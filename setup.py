#!/usr/bin/python3
from gttools import ovl, PSEXE
from array import array
import argparse
import gzip
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

DEFAULT_OUTPUT_NAME = 'GT2Combined.bin'

parser = argparse.ArgumentParser(description='Gran Turismo 2 Combined Disc install script. Running the script without any arguments will start Interactive Mode.')
parser.add_argument('-a', '--arcade-disc', type=str, dest='arcade_path', help='path to the Arcade Mode disc')
parser.add_argument('-s', '--simulation-disc', type=str, dest='sim_path', help='path to the Simulation Mode disc')
parser.add_argument('-o', '--output', type=str, dest='output_file', default=DEFAULT_OUTPUT_NAME, help='name of the output file (default: %(default)s)')
parser.add_argument('-e', '--ignore-errors', dest='ignore_errors', action='store_true', help='ignore non-critical errors encountered during setup. Critical errors are never ignored')

args = parser.parse_args()

interactive_mode = len(sys.argv) == 1

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def main():
    # Utils
    class SetupStepFailedError(ValueError):
        pass

    def getInputPath(prompt):
        import shlex

        while True:
            path = ''
            while len(path) == 0:
                path = input(prompt + ' ')

            display_path = shlex.split(path)[0]
            result = os.path.realpath(display_path)
            if os.path.isfile(result):
                return result
            print(f'{display_path!r} is not a valid file!')

    def getYesNoAnswer(prompt):
        # Copied from distutils.util since it's deprecated in 3.10 and will be removed in 3.12
        def strtobool(val):
            """Convert a string representation of truth to true (1) or false (0).
            True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
            are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
            'val' is anything else.
            """
            val = val.lower()
            if val in ('y', 'yes', 't', 'true', 'on', '1'):
                return 1
            elif val in ('n', 'no', 'f', 'false', 'off', '0'):
                return 0
            else:
                raise ValueError(f"invalid truth value {val!r}")

        while True:
            user_input = input(prompt + ' [y/n]: ')
            try:
                return bool(strtobool(user_input))
            except ValueError:
                print('Please use y/n or yes/no')

    def handleOptionalStepFailure():
        # TODO: Commandline argument to ignore errors if running non-interactive
        if interactive_mode:
            if not getYesNoAnswer('Continue anyway?'):
                sys.exit('Setup aborted by user.')
        else:
            if not args.ignore_errors:
                sys.exit('Setup aborted due to an error.')

    # GZIP stuff

    # This is very nasty, but there seems to be no other clean way to get the size
    # of an embedded gzip file, short of reimplementing the entire gzip module
    # So let's bruteforce its unpack by increasing size...
    def gzipDecompressBruteforce(data):
        size = 10
        while True:
            try:
                data_slice = data[:size]
                try:
                    d = gzip.decompress(data_slice)
                except EOFError:
                    # Grow the slice and retry
                    size += 1
                    continue
            except (IndexError, gzip.BadGzipFile):
                # No valid gzip?
                return None

            # data_slice is a valid gzip file, get the size and try to extract the filename if it exists!
            filename = None
            flags, = struct.unpack_from('<B', data_slice, 3)
            if flags & 8: # FNAME
                name_offset = 10
                if flags & 4: # FEXTRA
                    extra_size, = struct.unpack_from("<H", data_slice, name_offset)
                    name_offset += extra_size + 2
                filename = data[name_offset:].split(b'\x00', 1)[0].decode('latin-1')
            return d, size, filename

    def gzipCompressWithFilename(data, filename):
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='wb', filename=filename) as f:
            f.write(data)
        return buf.getvalue()

    # Patching stuff
    def getPattern(exe, pattern, offset=0):
        for match in re.finditer(pattern, exe.map):
            return exe.vaddr(match.start()) + offset
        return None

    # Set up paths
    MKPSXISO_PATH = os.path.realpath('tools/mkpsxiso')
    GTVOLTOOL_PATH = os.path.realpath('tools/GTVolTool')
    VOL_REPLACEMENTS_PATH = os.path.realpath('vol_replacements')
    MENU_ENTRIES_PATH = os.path.realpath('menu_entries')

    XML_NAME = 'files.xml'
    DISC_MODIFIED_TIMESTAMP = "2022022000000000+0"

    if interactive_mode:
        arcade_path = getInputPath('Input the path to GT2 Arcade Disc:')
        sim_path = getInputPath('Input the path to GT2 Simulation Disc:')
        output_file = input(f'Input the name of the output file (default: {DEFAULT_OUTPUT_NAME}): ') or DEFAULT_OUTPUT_NAME
    else:
        arcade_path = args.arcade_path
        sim_path = args.sim_path
        output_file = args.output_file

    with tempfile.TemporaryDirectory(prefix='gt2combined-', ignore_cleanup_errors=True) as temp_dir:

        # Installation steps
        def stepUnpackDiscs():
            dumpsxiso_exe_path = os.path.join(MKPSXISO_PATH, 'dumpsxiso')
            try:
                print('Unpacking the Arcade Mode disc...')
                arcade_dest = os.path.join(temp_dir, 'disc1')
                subprocess.run([dumpsxiso_exe_path, arcade_path, '-x', arcade_dest, '-s', os.path.join(arcade_dest, XML_NAME)], check=True)

                print('Unpacking the Simulation Mode disc...')
                sim_dest = os.path.join(temp_dir, 'disc2')
                subprocess.run([dumpsxiso_exe_path, sim_path, '-x', sim_dest, '-s', os.path.join(sim_dest, XML_NAME)], check=True)

            except subprocess.CalledProcessError as e:
                sys.exit(f'Unpacking discs failed with error {e.returncode}!')

            # Don't trust the user - check for real discs by checking for FAULTY.PSX on sim and STREAM.DAT on arcade
            real_arcade_path = None
            real_sim_path = None

            if os.path.isfile(os.path.join(arcade_dest, 'STREAM.DAT')):
                real_arcade_path = arcade_dest
            else:
                real_sim_path = arcade_dest
            
            if os.path.isfile(os.path.join(sim_dest, 'STREAM.DAT')):
                real_arcade_path = sim_dest
            else:
                real_sim_path = sim_dest

            if real_arcade_path is None or real_sim_path is None:
                sys.exit('Could not determine the disc types after unpacking! Did you unpack correct Arcade and Simulation discs?')
            if real_arcade_path == real_sim_path:
                sys.exit('Both discs contain STREAM.DAT! Did you unpack correct Arcade and Simulation discs?')
            return real_arcade_path, real_sim_path

        def stepPackDisc(path):
            print('Packing the combined disc...')
            try:
                arguments = [os.path.join(MKPSXISO_PATH, 'mkpsxiso'), os.path.join(path, XML_NAME)]
                if not interactive_mode:
                    arguments.append('-y')
                subprocess.run(arguments)
            except subprocess.CalledProcessError as e:
                sys.exit(f'Packing the disc failed with error {e.returncode}!')

        def stepUnpackVOL(path):
            print('Unpacking GT2.VOL from the Simulation Mode disc...')
            try:
                output_dir = os.path.join(path, 'vol')
                subprocess.run([os.path.join(GTVOLTOOL_PATH, "GTVolTool"), '-e2', os.path.join(path, 'GT2.VOL'), output_dir], check=True)
                return output_dir
            except subprocess.CalledProcessError as e:
                sys.exit(f'Unpacking GT2.VOL failed with error {e.returncode}!')

        def stepPackVOL(path, vol_files):
            print('Packing GT2.VOL...')
            try:
                subprocess.run([os.path.join(GTVOLTOOL_PATH, "GTVolTool"), '-r2', vol_files, os.path.join(path, 'GT2.VOL')])
            except subprocess.CalledProcessError as e:
                sys.exit(f'Packing GT2.VOL failed with error {e.returncode}!')

        def stepUnpackOVL(path):
            print('Unpacking GT2.OVL from the Simulation Mode disc...')

            output_dir = os.path.join(path, 'ovl')
            ovl.unpack(os.path.join(path, 'GT2.OVL'), output_dir)
            return output_dir

        def stepPackOVL(path, ovl_files):
            print('Packing GT2.OVL...')

            files_to_pack = [os.path.join(ovl_files, f'gt2_{(x+1):02}.exe') for x in range(6)]
            ovl.pack(files_to_pack, os.path.join(path, 'GT2.OVL'))

        def stepMergeXMLs(arcade_path, sim_path, output_file):
            print('Modifying the XML file...')

            arcade_xml = os.path.join(arcade_path, XML_NAME)
            sim_xml = os.path.join(sim_path, XML_NAME)
            try:
                arcade_tree = ET.parse(arcade_xml)
                sim_tree = ET.parse(sim_xml)

                arcade_project = arcade_tree.getroot()
                sim_project = sim_tree.getroot()

                sim_project.set('image_name', os.path.splitext(output_file)[0] + '.bin')
                sim_project.set('cue_sheet', os.path.splitext(output_file)[0] + '.cue')

                arcade_data_track = arcade_project.find("./track[@type='data']")
                sim_data_track = sim_project.find("./track[@type='data']")

                sim_identifiers = sim_data_track.find('identifiers')
                sim_identifiers.set('modification_date', DISC_MODIFIED_TIMESTAMP)

                arcade_streams = arcade_data_track.find("./directory_tree/file[@source='STREAM.DAT']")
                sim_faulty = sim_data_track.find("./directory_tree/file[@source='FAULTY.PSX']")

                sim_faulty.text = arcade_streams.text
                sim_faulty.attrib = arcade_streams.attrib

                # STREAMS.DAT needs an absolute path
                sim_faulty.set('source', os.path.join(arcade_path, sim_faulty.get('source')))

                sim_tree.write(sim_xml)
            except ET.ParseError as e:
                sys.exit(f'XML parse failure: {e}.')
            except (AttributeError, TypeError): # If any of the .find calls return None
                sys.exit('XML parse failure.')

        def stepPatchEboot(path):
            print('Patching the boot executable...')
            try:
                # "Support" all regions by reading SYSTEM.CNF
                with open(os.path.join(path, 'SYSTEM.CNF'), 'r') as f:
                    cnf = {}
                    for line in f:
                        key, value = line.split('=', 1)
                        cnf[key.strip()] = value.strip()
                eboot_name = cnf['BOOT'].removeprefix('cdrom:\\').rsplit(';', 1)[0]
                with PSEXE(os.path.join(path, eboot_name), readonly=False) as exe:
                    # Replace li $a0, 1 with 5li $a0, 5 in sub_8005D6E0 to re-enable intro videos
                    if immediate := getPattern(exe, rb'\x00\x00\x00\x00.{4}\x01\x00\x04\x24\x10\x00\xBF\x8F', 8): # Pointer to \x01
                        exe.writeU16(immediate, 5)
                    else: # No matches
                        sys.exit(f'Failed to locate code patterns in {eboot_name}. Your game version may be unsupported.')

            except (OSError, KeyError, IndexError):
                sys.exit('Failed to patch the boot executable!')

        def stepPatchMainMenuOverlay(path):
            print('Patching gt2_02.exe (main menu overlay)...')
            try:
                main_menu_overlay_path = os.path.join(path, 'gt2_02.exe')
                data_to_append = bytearray()
                with PSEXE(main_menu_overlay_path, readonly=False, headless=True, baseAddress=0x80010000) as exe:
                    added_data_vaddr_cursor = exe.vaddr(exe.map.size())

                    if menu_actions_process := getPattern(exe, rb'\x00\x00\x00\x00\x07\x00\x62\x2C', 4):
                        exe.writeU16(menu_actions_process, 8)

                        # Get the jump table ptr
                        menu_actions_jump_table_ptr = exe.readIndirectPtr(menu_actions_process+8, menu_actions_process+12)
                        # Before writing anything, read the original pointers as needed...
                        menu_action0_ptr = exe.readAddress(menu_actions_jump_table_ptr)
                        menu_action_attr_ptr_hi, menu_action_attr_ptr_lo = exe.readU32(menu_action0_ptr+4), exe.readU32(menu_action0_ptr+8)
                        menu_action_jal = exe.readU32(menu_action0_ptr+24)
                        menu_action_j = exe.readU32(menu_action0_ptr+32)

                        # Now assemble the code
                        menu_action7 = bytearray()
                        menu_action7.extend(b'\x02\x00\x04\x24') # li $a0, 2
                        menu_action7.extend(struct.pack('<II', menu_action_attr_ptr_hi, menu_action_attr_ptr_lo)) # la $v0, byte_801EF5F0
                        menu_action7.extend(b'\x01\x00\x03\x24') # li $v1, 1
                        menu_action7.extend(b'\x01\x00\x43\xA0') # sb $v1, 1($v0)
                        menu_action7.extend(struct.pack('<I', menu_action_jal)) # jal sub_8005DA3C
                        menu_action7.extend(b'\x02\x00\x43\xA0') # sb $v1, 2($v0)
                        menu_action7.extend(struct.pack('<I', menu_action_j)) # j def_800114CC
                        menu_action7.extend(b'\x00' * 4) # nop

                        data_to_append.extend(menu_action7)
                        exe.writeAddress(menu_actions_jump_table_ptr + 7*4, added_data_vaddr_cursor)
                        added_data_vaddr_cursor += len(menu_action7)
                    else:
                        raise SetupStepFailedError

                    if menu_actions_ptr := getPattern(exe, rb'.{2}\x02\x3C.{2}\x42\x24\x40\x18\x11\x00\x21\x18\x62\x00'):
                        menu_actions_off = exe.addr(exe.readIndirectPtr(menu_actions_ptr, menu_actions_ptr+4))
                        struct.pack_into('<9h', exe.map, menu_actions_off, -1, 7, 0, 1, 2, 3, 4, 5, -1)
                    else:
                        raise SetupStepFailedError

                    if draw_menu_entries := getPattern(exe, rb'\x21\x20\x00\x02.{2}\x05\x3C.{6}\xA5\x24.{2}\x04\x3C.{2}\x84\x24\x80\x28\x12\x00'):

                        # Relocate 0x20800F
                        unk_menu_defs_off = exe.addr(exe.readIndirectPtr(draw_menu_entries+4, draw_menu_entries+12))
                        unk_menu_defs = exe.map[unk_menu_defs_off:unk_menu_defs_off+4]
                        data_to_append.extend(unk_menu_defs)
                        exe.writeIndirectPtr(draw_menu_entries+4, draw_menu_entries+12, added_data_vaddr_cursor)
                        added_data_vaddr_cursor += len(unk_menu_defs)

                        # Expand and move the textures array
                        main_menu_textures_off = exe.addr(exe.readIndirectPtr(draw_menu_entries+28, draw_menu_entries+32))
                        main_menu_textures_off += 2
                        exe.writeIndirectPtr(draw_menu_entries+28, draw_menu_entries+32, exe.vaddr(main_menu_textures_off))
                        struct.pack_into('<9h', exe.map, main_menu_textures_off, 0, 0, 1, 2, 3, 4, 5, 6, 0)

                        # Expand and move the main menu definitions
                        # We change an array of 7 langs x 6 entries to 6 langs x 7 entries + 7th language gets appended
                        def readMenuDefinitions(file):
                            with open(os.path.join(MENU_ENTRIES_PATH, file + '.bin'), 'rb') as f:
                                return f.read()

                        menu_item_definitions_array_ptr = exe.readIndirectPtr(draw_menu_entries+16, draw_menu_entries+20)
                        menu_item_definitions_buffer_offset = exe.addr(exe.readAddress(menu_item_definitions_array_ptr))

                        # Misc (Data Transfer box) comes directly before menu_item_definitions_buffer
                        exe.map[menu_item_definitions_buffer_offset-12:menu_item_definitions_buffer_offset] = readMenuDefinitions('misc')
                        for lang in ('jp', 'en-us', 'en-uk', 'fr', 'it', 'de'):
                            exe.map[menu_item_definitions_buffer_offset:menu_item_definitions_buffer_offset + (7*12)] = readMenuDefinitions(lang)
                            exe.writeAddress(menu_item_definitions_array_ptr, exe.vaddr(menu_item_definitions_buffer_offset))

                            menu_item_definitions_array_ptr += 4
                            menu_item_definitions_buffer_offset += 7*12

                        data_to_append.extend(readMenuDefinitions('es'))
                        exe.writeAddress(menu_item_definitions_array_ptr, added_data_vaddr_cursor)
                        added_data_vaddr_cursor += 7*12
                    else:
                        raise SetupStepFailedError

                    if num_menu_entries_ptr := getPattern(exe, rb'.{2}\x03\x3C.{2}\x10\x3C.{2}\x10\x26\x21\x20\x00\x02', 4):
                        num_menu_entries = exe.readIndirectPtr(num_menu_entries_ptr, num_menu_entries_ptr+4)
                        exe.writeU16(num_menu_entries, 9)
                    else:
                        raise SetupStepFailedError

                    if num_menu_clamp := getPattern(exe, rb'\x06\x00\x06\x24\xFD\xFF\x02\x24'):
                        exe.writeU16(num_menu_clamp, 7)
                    else:
                        raise SetupStepFailedError

                # If there is any data to append, do so
                if len(data_to_append) > 0:
                    with open(main_menu_overlay_path, 'ab') as f:
                        f.write(data_to_append)

            except (OSError, IndexError, SetupStepFailedError):
                sys.exit('Failed to patch gt2_02.exe!')

        def stepReplaceCoreVOLFiles(path):
            print("Replacing core VOL files... Those files are replaced even if they don't match the originals.")
            try:
                shutil.copytree(os.path.join(VOL_REPLACEMENTS_PATH, 'core'), path, dirs_exist_ok=True)
            except shutil.Error:
                sys.exit('Failed to replace core VOL files!')

        def patchTXD(path):
            try:
                with open(path, 'rb') as f:
                    buf = f.read()

                replacements = [
                    (b'Obtain Licences in Disk 2 to Access All Courses', 0x3B, b'Obtain Licenses in Simulation Mode to Access All Courses')
                ]
                for replacement in replacements:
                    search_pattern = replacement[0].ljust(replacement[1], b'\0')
                    replace_pattern = replacement[2].ljust(replacement[1], b'\0')

                    buf = buf.replace(search_pattern, replace_pattern, 1)

                with open(path, 'wb') as f:
                    f.write(buf)
            except OSError:
                raise SetupStepFailedError()

        def stepPatchRaceTXD(path):
            print('Patching data-race.txd...')
            try:
                patchTXD(os.path.join(path, '.text', 'data-race.txd'))
            except SetupStepFailedError:
                eprint('Patching data-race.txd failed!')
                handleOptionalStepFailure()

        def stepPatchArcadeTXD(path):
            print('Patching data-arcade.txd inside gt2_03.exe (arcade mode overlay)...')
            try:
                arcade_overlay_path = os.path.join(path, 'gt2_03.exe')
                data_to_append = bytearray()
                with PSEXE(arcade_overlay_path, readonly=False, headless=True, baseAddress=0x80010000) as exe:
                    if data_arcade_gzip_ptr := getPattern(exe, rb'.{2}\x04\x3C.{2}\x84\x24'):
                        vaddr = exe.readIndirectPtr(data_arcade_gzip_ptr, data_arcade_gzip_ptr+4)

                        orig_gzip_location = exe.addr(vaddr)
                        data, orig_gzip_size, orig_gzip_filename = gzipDecompressBruteforce(exe.map[orig_gzip_location:])
                    else: # No matches
                        raise SetupStepFailedError

                    txd_filename = orig_gzip_filename if orig_gzip_filename else 'data-arcade.txd'
                    txd_path = os.path.join(path, txd_filename)
                    with open(txd_path, 'wb') as f:
                        f.write(data)

                    patchTXD(txd_path)

                    with open(txd_path, 'rb') as f:
                        data = f.read()

                    compressed_data = gzipCompressWithFilename(data, txd_filename)
                    # If compressed data is larger than the original, try again with filename stripped
                    if len(compressed_data) > orig_gzip_size:
                        compressed_data = gzip.compress(data)

                    # Wipe the original gzip location before overwriting or appending
                    exe.map[orig_gzip_location:orig_gzip_location+orig_gzip_size] = b'\0' * orig_gzip_size
                    if len(compressed_data) > orig_gzip_size:
                        # Append
                        vaddr = exe.vaddr(exe.map.size())
                        exe.writeIndirectPtr(data_arcade_gzip_ptr, data_arcade_gzip_ptr+4, vaddr)
                        data_to_append.extend(compressed_data)
                    else:
                        # Replace
                        exe.map[orig_gzip_location:orig_gzip_location+len(compressed_data)] = compressed_data

                # If there is any data to append, do so
                if len(data_to_append) > 0:
                    with open(arcade_overlay_path, 'ab') as f:
                        f.write(data_to_append)

            except (OSError, IndexError, SetupStepFailedError):
                eprint('Patching data-arcade.txd inside gt2_03.exe failed!')
                handleOptionalStepFailure()

        # Those all call sys.exit on failure
        arcade_files, sim_files = stepUnpackDiscs()

        vol_files = stepUnpackVOL(sim_files)
        ovl_files = stepUnpackOVL(sim_files)

        stepMergeXMLs(arcade_files, sim_files, output_file)

        stepPatchEboot(sim_files)
        stepPatchMainMenuOverlay(ovl_files)
        stepReplaceCoreVOLFiles(vol_files)

        stepPatchRaceTXD(vol_files) # Optional step
        stepPatchArcadeTXD(ovl_files) # Optional step

        stepPackOVL(sim_files, ovl_files)
        stepPackVOL(sim_files, vol_files)
        stepPackDisc(sim_files)

        print('Setup completed successfully!')

try:
    main()
except BaseException as e:
    eprint(e)

if interactive_mode:
    input('Press any key to exit...')
