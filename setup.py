import gzip
import struct
import subprocess
import tempfile
import os
import io
import sys
import argparse
import re
from gttools import ovl, PSEXE

parser = argparse.ArgumentParser(description='Gran Turismo 2 Combined Disc install script. Running the script without any arguments will start Interactive Mode.')
parser.add_argument('-a', '--arcade-disc', type=str, dest='arcade_path', help='path to the Arcade Mode disc')
parser.add_argument('-s', '--simulation-disc', type=str, dest='sim_path', help='path to the Simulation Mode disc')

args = parser.parse_args()

interactive_mode = len(sys.argv) == 1

def main():
    # Utils
    class SetupStepFailedError(ValueError):
        pass

    def eprint(*args, **kwargs):
        print(*args, file=sys.stderr, **kwargs)

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
            print(f'{display_path} is not a valid file!')

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
                raise ValueError("invalid truth value %r" % (val,))

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
                sys.exit('Setup aborted by user')

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
            flags, = struct.unpack_from('B', data_slice, 3)
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

    # Set up paths
    MKPSXISO_PATH = os.path.realpath('tools/mkpsxiso')
    GTVOLTOOL_PATH = os.path.realpath('tools/GTVolTool')

    if interactive_mode:
        arcade_path = getInputPath('Input the path to GT2 Arcade Disc:')
        sim_path = getInputPath('Input the path to GT2 Simulation Disc:')
    else:
        arcade_path = args.arcade_path
        sim_path = args.sim_path

    with tempfile.TemporaryDirectory(prefix='gt2combined-', ignore_cleanup_errors=True) as temp_dir:

        # Installation steps
        def stepUnpackDiscs():
            dumpsxiso_exe_path = os.path.join(MKPSXISO_PATH, 'dumpsxiso')
            try:
                print('Unpacking the Arcade Mode disc...')
                arcade_dest = os.path.join(temp_dir, 'disc1')
                subprocess.run([dumpsxiso_exe_path, arcade_path, '-x', arcade_dest, '-s', os.path.join(arcade_dest, 'files.xml')], check=True)

                print('Unpacking the Simulation Mode disc...')
                sim_dest = os.path.join(temp_dir, 'disc2')
                subprocess.run([dumpsxiso_exe_path, sim_path, '-x', sim_dest, '-s', os.path.join(sim_dest, 'files.xml')], check=True)

            except subprocess.CalledProcessError as e:
                sys.exit(f'Unpacking discs failed with error {e.returncode}!')

            # Don't trust the user - check for real discs by checking for FAULTY.PSX on sim and STREAM.DAT on arcade
            real_arcade_path = None
            real_sim_path = None

            if os.path.isfile(os.path.join(arcade_dest, 'STREAM.DAT')):
                real_arcade_path = arcade_dest
            elif os.path.isfile(os.path.join(sim_dest, 'STREAM.DAT')):
                real_arcade_path = sim_dest

            if os.path.isfile(os.path.join(arcade_dest, 'FAULTY.PSX')):
                real_sim_path = arcade_dest
            elif os.path.isfile(os.path.join(sim_dest, 'FAULTY.PSX')):
                real_sim_path = sim_dest

            if real_arcade_path is None or real_sim_path is None:
                sys.exit('Could not determine the disc types after unpacking! Did you unpack correct Arcade and Simulation discs?')
            return real_arcade_path, real_sim_path

        def unpackVOL(path):
            print('Unpacking GT2.VOL from the Simulation Mode disc...')
            try:
                output_dir = os.path.join(path, 'vol')
                subprocess.run([os.path.join(GTVOLTOOL_PATH, "GTVolTool"), '-e2', os.path.join(path, 'GT2.VOL'), output_dir], check=True)
                return output_dir
            except subprocess.CalledProcessError as e:
                sys.exit(f'Unpacking GT2.VOL failed with error {e.returncode}!')

        def unpackOVL(path):
            print('Unpacking GT2.OVL from the Simulation Mode disc...')

            output_dir = os.path.join(path, 'ovl')
            ovl.unpack(os.path.join(path, 'GT2.OVL'), output_dir)
            return output_dir

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

                    buf = buf.replace(search_pattern, replace_pattern)

                with open(path, 'wb') as f:
                    f.write(buf)
            except OSError:
                raise SetupStepFailedError()

        def patchRaceTXD(path):
            try:
                patchTXD(os.path.join(path, '.text', 'data-race.txd'))
            except SetupStepFailedError:
                eprint('Patching data-race.txd failed!')
                handleOptionalStepFailure()

        def patchArcadeTXD(path):
            try:
                arcade_overlay_path = os.path.join(path, 'gt2_03.exe')
                data_to_append = bytearray()
                with PSEXE(arcade_overlay_path, readonly=False, headless=True, baseAddress=0x80010000) as exe:
                    try:
                        for match in re.finditer(rb'.{2}\x04\x3C.{2}\x84\x24', exe.map):
                            data_arcade_gzip_ptr = exe.vaddr(match.start())
                            vaddr = exe.readIndirectPtr(data_arcade_gzip_ptr, data_arcade_gzip_ptr+4)

                            orig_gzip_location = exe.addr(vaddr)
                            data, orig_gzip_size, orig_gzip_filename = gzipDecompressBruteforce(exe.map[orig_gzip_location:])
                            break
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
                            exe.writeIndirectRef(data_arcade_gzip_ptr, data_arcade_gzip_ptr+4, vaddr)
                            data_to_append.extend(compressed_data)
                        else:
                            # Replace
                            exe.map[orig_gzip_location:orig_gzip_location+len(compressed_data)] = compressed_data

                    finally:
                        # Explicitly release references to the map
                        data_arcade_gzip_ptr = None
                
                # If there is any data to append, do so
                if len(data_to_append) > 0:
                    with open(arcade_overlay_path, 'ab') as f:
                        f.write(data_to_append)

            except (OSError, SetupStepFailedError):
                eprint('Patching data-arcade.txd in the arcade overlay failed!')
                handleOptionalStepFailure()

        # Those all call sys.exit on failure
        arcade_files, sim_files = stepUnpackDiscs()
        vol_files = unpackVOL(sim_files)
        ovl_files = unpackOVL(sim_files)

        # File patching
        patchRaceTXD(vol_files) # Optional step

        # OVL patching
        patchArcadeTXD(ovl_files) # Optional step

        os._exit(0) # Tmp

try:
    main()
finally:
    if interactive_mode:
        input('Press any key to exit...')
