#!/usr/bin/python3
from gttools import ovl, PSEXE
from array import array
import argparse
import binascii
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
parser.add_argument('-f', '--no-fmvs', dest='no_fmvs', action='store_true', help='Do not include movies in the combined disc. This leaves out the intro movie, credits and track previews in Arcade Mode, but allows the disc to be burned on a CD.')
parser.add_argument('-e', '--ignore-errors', dest='ignore_errors', action='store_true', help='ignore non-critical errors encountered during setup. Critical errors are never ignored')
parser.add_argument('-t', '--text-only', dest='text_only', action='store_true', help='Do not use native filepicker windows.')

cur_python_version, required_python_version = sys.version_info[:3], (3, 10, 0)
if not cur_python_version >= required_python_version:
    sys.exit(f"Your Python version {'.'.join(str(i) for i in cur_python_version)} is too old. Please update to Python {'.'.join(str(i) for i in required_python_version)} or newer.")

args = parser.parse_args()
interactive_mode = len(tuple(a for a in sys.argv if a not in ("-t", "--text-only"))) == 1
gui_mode = not args.text_only
if gui_mode:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        gui_mode = False

if interactive_mode:
    print(
"""
Gran Turismo 2 Combined Disc install script.
Running the script in Interactive Mode.

After paths to Arcade and Simulation discs are given, this script will unpack both discs and the VOL file from the Simulation Disc, then patch code and asset.
The setup process may take some time, so please be patient and don't close this window even if the process seems stuck.
""")
if gui_mode:
    print("About to open dialogs file paths (re-run script with -t to run without dialogs).")
    if os.name == 'nt':
        os.system("pause")
    else:
        os.system('read -s -n 1 -p "Press any key to continue ..."')
    try:
        root = tk.Tk()  # init Tk to make filedialog work
        root.withdraw()  # withdraw because root window not needed
    except Exception as e:
        gui_mode = False
        print(f"Forcing text-only mode because of problem initializing Tkinter GUI: {e.__repr__()}", file=sys.stderr)

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def getResourcePath(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def main():
    # Utils
    class SetupStepFailedError(ValueError):
        pass

    def getOutputPath(prompt, default):
        prompt_cli = prompt + f' ({default=}): '
        while True:
            if gui_mode:
                filename = filedialog.asksaveasfilename(confirmoverwrite=False, initialfile=default, title=prompt, defaultextension=".bin", filetypes=(("BIN file", ""), ))
            else:
                filename = getResourcePath(input(prompt_cli) or default)
            if not filename:
                sys.exit('Setup aborted by user.')
            elif os.path.isfile(filename):
                print(f'{filename!r} already exists!')
            else:
                try:
                    open(filename, 'w+').close()
                    os.unlink(filename)
                    return filename
                except OSError:
                    print(f'{filename!r} is not writable!')

    def getInputPath(prompt):
        if gui_mode:
            while True:
                filename = filedialog.askopenfilename(title=prompt)
                if not filename:
                    sys.exit('Setup aborted by user.')
                if os.path.isfile(filename):
                    return filename
                print(f'{filename!r} is not a valid file!')
        else:
            import shlex
            while True:
                path = ''
                while len(path) == 0:
                    path = input(prompt + ' ')
                display_path = shlex.split(path)[0]
                result = getResourcePath(display_path)
                if os.path.isfile(result):
                    return result
                print(f'{display_path!r} is not a valid file!')

    def getYesNoAnswer(prompt, default=None):
        # Copied from distutils.util since it's deprecated in 3.10 and will be removed in 3.12
        def strtobool(val):
            """Convert a string representation of truth to True or False.
            True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
            are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
            'val' is anything else.
            """
            val = val.lower()
            if val in ('y', 'yes', 't', 'true', 'on', '1'):
                return True
            elif val in ('n', 'no', 'f', 'false', 'off', '0'):
                return False
            else:
                raise ValueError(f"invalid truth value {val!r}")

        try:
            default_return = strtobool(default)
            default_str = ' [Y/n]: ' if default_return else ' [y/N]: '
        except ValueError:
            default_return = None
            default_str = ' [y/n]: '

        while True:
            user_input = input(prompt + default_str)
            try:
                return strtobool(user_input)
            except ValueError:
                if default_return is None:
                    print('Please use y/n or yes/no')
                else:
                    return default_return

    def optionalStepFailed(text):
        eprint(text)
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
            except (LookupError, gzip.BadGzipFile):
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

    def alignByteArray(buf, offset, alignment):
        mask = -alignment & 0xffffffff
        aligned_offset = (offset + (alignment - 1)) & mask
        buf.extend(b'\x00' * (aligned_offset - offset))
        return aligned_offset

    # Set up paths
    MKPSXISO_PATH = getResourcePath('tools/mkpsxiso')
    GTVOLTOOL_PATH = getResourcePath('tools/GTVolTool')
    VOL_REPLACEMENTS_PATH = getResourcePath('vol_replacements')
    MENU_ENTRIES_PATH = getResourcePath('menu_entries')

    XML_NAME = 'files.xml'
    DISC_MODIFIED_TIMESTAMP = "2022022600000000+0"

    if interactive_mode:
        arcade_path = getInputPath('Enter the path to GT2 Arcade Disc (.bin file):')
        sim_path = getInputPath('Enter the path to GT2 Simulation Disc (.bin file):')
        output_file = getOutputPath("Enter the location to save the output file", DEFAULT_OUTPUT_NAME)
        no_fmvs = not getYesNoAnswer('Include FMVs in the combined disc? If selected, the combined disc will have the intro movie, '
                'credits and track previews in Arcade Mode, but it will be too big to burn on a CD. Select No if you want to use the combined disc on a console, Yes otherwise.', default='yes')
    else:
        arcade_path = args.arcade_path
        sim_path = args.sim_path
        no_fmvs = args.no_fmvs
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

                if not no_fmvs:
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
            # If we don't want FMVs, there is nothing to patch in the main executable
            if no_fmvs:
                return

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
                        raise SetupStepFailedError

            except (OSError, LookupError):
                sys.exit('Failed to patch the boot executable!')
            except SetupStepFailedError:
                sys.exit(f'Failed to locate code patterns in {eboot_name}! Your game version may be unsupported.')

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

                        added_data_vaddr_cursor = alignByteArray(data_to_append, added_data_vaddr_cursor, 4)
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

                        added_data_vaddr_cursor = alignByteArray(data_to_append, added_data_vaddr_cursor, 4)
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
                        for lang in ('jp', 'en-us', 'en-uk', 'fr', 'de', 'it'):
                            exe.map[menu_item_definitions_buffer_offset:menu_item_definitions_buffer_offset + (7*12)] = readMenuDefinitions(lang)
                            exe.writeAddress(menu_item_definitions_array_ptr, exe.vaddr(menu_item_definitions_buffer_offset))

                            menu_item_definitions_array_ptr += 4
                            menu_item_definitions_buffer_offset += 7*12

                        added_data_vaddr_cursor = alignByteArray(data_to_append, added_data_vaddr_cursor, 4)
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

            except (OSError, LookupError):
                sys.exit('Failed to patch gt2_02.exe!')
            except SetupStepFailedError:
                sys.exit('Failed to locate code patterns in gt2_02.exe! Your game version may be unsupported.')

        def stepPatchRaceOverlay(path):
            # If we don't want FMVs, there is nothing to patch in gt2_01.exe
            if no_fmvs:
                return

            print('Patching gt2_01.exe (race overlay)...')
            try:
                main_menu_overlay_path = os.path.join(path, 'gt2_01.exe')
                with PSEXE(main_menu_overlay_path, readonly=False, headless=True, baseAddress=0x80010000) as exe:

                    # Those seem to be related to playing FMVs, but I don't know if it's actually used
                    # Patching it anyway to match Arcade just in case
                    if set_s3_to_5 := getPattern(exe, rb'\x10\x00\xB0\xAF\x01\x00\x13\x24', 4):
                        exe.writeU16(set_s3_to_5, 5)
                    else:
                        raise SetupStepFailedError

                    if compare_against_v0_1 := getPattern(exe, rb'\x00\x00\x00\x00.{2}\x73\x10'):
                        # li v0, 1
                        exe.writeU32(compare_against_v0_1, int.from_bytes(b'\x01\x00\x02\x24', byteorder='little', signed=False))
                        # Change beq $v1, $s3 to beq $v1, $v0
                        exe.writeU16(compare_against_v0_1+6, int.from_bytes(b'\x62\x10', byteorder='little', signed=False))
                    else:
                        raise SetupStepFailedError

                    if set_a0_to_0 := getPattern(exe, rb'\x01\x00\x04\x24\x10\x00\xBF\x8F'):
                        exe.writeU16(set_a0_to_0, 5)
                    else:
                        raise SetupStepFailedError

            except (OSError, LookupError):
                sys.exit('Failed to patch gt2_01.exe!')
            except SetupStepFailedError:
                sys.exit('Failed to locate code patterns in gt2_01.exe! Your game version may be unsupported.')

        def stepReplaceVOLFiles(path):
            print("Replacing VOL files...")
            try:
                hashes = {}
                # Handle absence of the .hashes file gracefully in case the user removed it
                try:
                    with open(os.path.join(VOL_REPLACEMENTS_PATH, 'file.hashes'), 'r') as f:
                        for line in f:
                            value, key = line.split('=', 1)
                            # Path : Hash dictionary
                            hashes[os.path.normcase(key.strip())] = int(value.strip(), 16)
                except OSError:
                    print('Warning: file.hashes has been removed or cannot be read.')

                for root, _, files in os.walk(VOL_REPLACEMENTS_PATH):
                    for file in files:
                        if file == 'file.hashes':
                            continue

                        copy_file = True
                        absolute_src_path = os.path.join(root, file)
                        relative_src_path = os.path.normcase(os.path.relpath(absolute_src_path, VOL_REPLACEMENTS_PATH))

                        absolute_dst_path = os.path.join(path, relative_src_path)
                        expected_hash = hashes.get(relative_src_path)
                        if expected_hash is not None:
                            # If the file is a GZIP file, decompress first as we're interested in CRC32 of the contents, not archive
                            try:
                                with gzip.open(absolute_dst_path, 'rb') as f:
                                    actual_hash = binascii.crc32(f.read()) & 0xFFFFFFFF
                            except gzip.BadGzipFile:
                                # Try uncompressed
                                with open(absolute_dst_path, 'rb') as f:
                                    actual_hash = binascii.crc32(f.read()) & 0xFFFFFFFF

                            copy_file = expected_hash == actual_hash

                        if copy_file:
                            shutil.copy2(absolute_src_path, absolute_dst_path)
                        else:
                            print(f'Warning: {relative_src_path} was not overwritten as it is already modified.')

            except (OSError, SetupStepFailedError, shutil.Error):
                sys.exit('Failed to replace VOL files!')

        def patchTXD(path):
            with open(path, 'rb') as f:
                buf = f.read()

            replacements = [
                # Race strings
                (b'in the ARCADE MODE DISC', 29, b'in ARCADE MODE'),
                (b'on ARCADE MODE DISC', 29, b'in ARCADE MODE'),
                (b'sur le CD du MODE ARCADE', 29, b'dans le MODE ARCADE'),
                (b'auf der ARCADE-MODUS-CD', 29, b'im ARCADE-MODUS'),
                (b'nel DISCO A  MODALIT\xC0 ARCADE', 29, b'nella MODALIT\xC0 ARCADE'),
                (b'en el DISCO DE MODO ARCADE', 29, b'en el MODO ARCADE'),

                # Arcade strings
                (b'Obtain Licences in Disk 2 to Access All Courses', 59, b'Obtain Licenses in Simulation Mode to Access All Courses'),
                (b'Obtain Licences in GT mode to Access All Courses', 59, b'Obtain Licences in GT Mode to Access All Courses'),
                (b'Passer permis du CD 2 pour participer aux \xE9preuves', 59, b'Passer les permis du Mode GT pour d\xE9bloquer les circuits'),
                (b'Erwerben Sie Lizenzen f\x6Er alle Strecken auf CD 2', 59, b'Erwerben Sie Lizenzen f\x6Er alle Strecken im GT-Modus'),
                (b'Ottieni le patenti nel Disco 2 e accedi a tutti i percorsi', 59, b'Ottieni le patenti nella Modalit\xE0 GT e accedi ai percorsi'),
                (b'Obtenga carnes del Disco 2 para correr', 59, b'Obtenga carnes en GT Modo para correr')
            ]
            for replacement in replacements:
                search_pattern = replacement[0].ljust(replacement[1], b'\0')
                replace_pattern = replacement[2].ljust(replacement[1], b'\0')
                if len(search_pattern) > replacement[1] or len(replace_pattern) > replacement[1]:
                    raise SetupStepFailedError

                buf = buf.replace(search_pattern, replace_pattern, 1)

            with open(path, 'wb') as f:
                f.write(buf)

        def stepPatchRaceTXD(path):
            print('Patching data-race.txd...')
            try:
                patchTXD(os.path.join(path, '.text', 'data-race.txd'))
            except (OSError, SetupStepFailedError):
                optionalStepFailed('Patching data-race.txd failed!')

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

                        vaddr = alignByteArray(data_to_append, vaddr, 4)
                        exe.writeIndirectPtr(data_arcade_gzip_ptr, data_arcade_gzip_ptr+4, vaddr)
                        data_to_append.extend(compressed_data)
                    else:
                        # Replace
                        exe.map[orig_gzip_location:orig_gzip_location+len(compressed_data)] = compressed_data

                # If there is any data to append, do so
                if len(data_to_append) > 0:
                    with open(arcade_overlay_path, 'ab') as f:
                        f.write(data_to_append)

            except (OSError, LookupError):
                optionalStepFailed('Patching data-arcade.txd inside gt2_03.exe failed!')
            except SetupStepFailedError:
                optionalStepFailed('Failed to locate code patterns in gt2_03.exe! Your game version may be unsupported.')

        # Those all call sys.exit on failure
        arcade_files, sim_files = stepUnpackDiscs()

        vol_files = stepUnpackVOL(sim_files)
        ovl_files = stepUnpackOVL(sim_files)

        stepMergeXMLs(arcade_files, sim_files, output_file)

        stepPatchEboot(sim_files)
        stepPatchMainMenuOverlay(ovl_files)
        stepPatchRaceOverlay(ovl_files)
        stepReplaceVOLFiles(vol_files)

        stepPatchRaceTXD(vol_files) # Optional step
        stepPatchArcadeTXD(ovl_files) # Optional step

        stepPackOVL(sim_files, ovl_files)
        stepPackVOL(sim_files, vol_files)
        stepPackDisc(sim_files)

    print('Setup completed successfully!')

try:
    main()
except Exception as e:
    eprint(f'{type(e).__name__}: {e}')
except SystemExit as e:
    eprint(f'Error: {e}')

if interactive_mode:
    input('\nPress any key to exit...')
