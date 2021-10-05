import json
import logging
import os
import time

import archinstall
from archinstall.lib.general import run_custom_user_commands, SysCommand
from archinstall.lib.hardware import has_uefi, AVAILABLE_GFX_DRIVERS
from archinstall.lib.installer import Installer
from archinstall.lib.networking import check_mirror_reachable
from archinstall.lib.profiles import Profile
from archinstall.lib.user_interaction import get_password

if archinstall.arguments.get('help'):
	print("See `man archinstall` for help.")
	exit(0)
if os.getuid() != 0:
	print("Archinstall requires root privileges to run. See --help for more.")
	exit(1)

# For support reasons, we'll log the disk layout pre installation to match against post-installation layout
archinstall.log(f"Disk states before installing: {archinstall.disk_layouts()}", level=logging.DEBUG)

keyboard_language = 'us'
mirror_region = 'United States'
keep_partitions = False
filesystem_format = 'ext4'
encryption_password = None
bootloader = 'systemd-bootctl'
root_password = None
username = 'echo'
profile = 'gnome'

print('aur-packages', archinstall.arguments.get('aur-packages', None))

def ask_user_questions():
	"""
		First, we'll ask the user for a bunch of user input.
		Not until we're satisfied with what we want to install
		will we continue with the actual installation steps.
	"""
	if not archinstall.arguments.get('keyboard-language', None):
		while True:
			try:
				archinstall.arguments['keyboard-language'] = keyboard_language
				archinstall.log(f'Keyboard language: {keyboard_language}', fg='yellow')
				break
			except archinstall.RequirementError as err:
				archinstall.log(err, fg="red")

	# Before continuing, set the preferred keyboard layout/language in the current terminal.
	# This will just help the user with the next following questions.
	if len(archinstall.arguments['keyboard-language']):
		archinstall.set_keyboard_language(archinstall.arguments['keyboard-language'])

	# Set which region to download packages from during the installation
	if not archinstall.arguments.get('mirror-region', None):
		while True:
			try:
				archinstall.arguments['mirror-region'] = {mirror_region: archinstall.list_mirrors()[mirror_region]}
				archinstall.log(f'Mirror region: {mirror_region}', fg='yellow')
				break
			except archinstall.RequirementError as e:
				archinstall.log(e, fg="red")
	else:
		selected_region = archinstall.arguments['mirror-region']
		archinstall.log(f'Mirror region: {selected_region}', fg='yellow')
		archinstall.arguments['mirror-region'] = {selected_region: archinstall.list_mirrors()[selected_region]}

	if not archinstall.arguments.get('sys-language', None) and archinstall.arguments.get('advanced', False):
		archinstall.arguments['sys-language'] = input("Enter a valid locale (language) for your OS, (Default: en_US): ").strip()
		archinstall.arguments['sys-encoding'] = input("Enter a valid system default encoding for your OS, (Default: utf-8): ").strip()
		archinstall.log("Keep in mind that if you want multiple locales, post configuration is required.", fg="yellow")

	if not archinstall.arguments.get('sys-language', None):
		archinstall.arguments['sys-language'] = 'en_US'
	if not archinstall.arguments.get('sys-encoding', None):
		archinstall.arguments['sys-encoding'] = 'utf-8'

	language = archinstall.arguments['sys-language'] + '.' + archinstall.arguments['sys-encoding']
	archinstall.log(f'System language: {language}', fg='yellow')

	# Ask which harddrive/block-device we will install to
	archinstall.log(f'Block device: {archinstall.arguments.get("harddrive", None)}', fg='yellow')
	if archinstall.arguments.get('harddrive', None):
		archinstall.arguments['harddrive'] = archinstall.all_disks()[archinstall.arguments['harddrive']]
	else:
		archinstall.arguments['harddrive'] = archinstall.select_disk(archinstall.all_disks())
		if archinstall.arguments['harddrive'] is None:
			archinstall.arguments['target-mount'] = archinstall.storage.get('MOUNT_POINT', '/mnt')

	# Perform a quick sanity check on the selected harddrive.
	# 1. Check if it has partitions
	# 3. Check that we support the current partitions
	# 2. If so, ask if we should keep them or wipe everything
	if archinstall.arguments['harddrive'] and archinstall.arguments['harddrive'].has_partitions():
		# We curate a list pf supported partitions
		# and print those that we don't support.
		if not keep_partitions:
			archinstall.log(f'WARNING: Formatting everything on selected drive', fg='red')

		partition_mountpoints = {}
		for partition in archinstall.arguments['harddrive']:
			try:
				if partition.filesystem_supported():
					if keep_partitions:
						archinstall.log(f" {partition}")
					partition_mountpoints[partition] = None
			except archinstall.UnknownFilesystemFormat as err:
				if keep_partitions:
					archinstall.log(f" {partition} (Filesystem not supported)", fg='red')

		# We then ask what to do with the partitions.
		option = 'keep-existing' if keep_partitions else 'format-all'
		if option == 'abort':
			archinstall.log("Safely aborting the installation. No changes to the disk or system has been made.")
			exit(1)
		elif option == 'keep-existing':
			archinstall.arguments['harddrive'].keep_partitions = True

			archinstall.log(" ** You will now select which partitions to use by selecting mount points (inside the installation). **")
			archinstall.log(" ** The root would be a simple / and the boot partition /boot (as all paths are relative inside the installation). **")
			mountpoints_set = []
			while True:
				# Select a partition
				# If we provide keys as options, it's better to convert them to list and sort before passing
				mountpoints_list = sorted(list(partition_mountpoints.keys()))
				partition = archinstall.generic_select(mountpoints_list, "Select a partition by number that you want to set a mount-point for (leave blank when done): ")
				if not partition:
					if set(mountpoints_set) & {'/', '/boot'} == {'/', '/boot'}:
						break

					continue

				# Select a mount-point
				mountpoint = input(f"Enter a mount-point for {partition}: ").strip(' ')
				if len(mountpoint):

					# Get a valid & supported filesystem for the partition:
					while 1:
						new_filesystem = input(f"Enter a valid filesystem for {partition} (leave blank for {partition.filesystem}): ").strip(' ')
						if len(new_filesystem) <= 0:
							if partition.encrypted and partition.filesystem == 'crypto_LUKS':
								old_password = archinstall.arguments.get('!encryption-password', None)
								if not old_password:
									old_password = input(f'Enter the old encryption password for {partition}: ')

								if autodetected_filesystem := partition.detect_inner_filesystem(old_password):
									new_filesystem = autodetected_filesystem
								else:
									archinstall.log("Could not auto-detect the filesystem inside the encrypted volume.", fg='red')
									archinstall.log("A filesystem must be defined for the unlocked encrypted partition.")
									continue
							break

						# Since the potentially new filesystem is new
						# we have to check if we support it. We can do this by formatting /dev/null with the partitions filesystem.
						# There's a nice wrapper for this on the partition object itself that supports a path-override during .format()
						try:
							partition.format(new_filesystem, path='/dev/null', log_formatting=False, allow_formatting=True)
						except archinstall.UnknownFilesystemFormat:
							archinstall.log(f"Selected filesystem is not supported yet. If you want archinstall to support '{new_filesystem}',")
							archinstall.log("please create a issue-ticket suggesting it on github at https://github.com/archlinux/archinstall/issues.")
							archinstall.log("Until then, please enter another supported filesystem.")
							continue
						except archinstall.SysCallError:
							pass  # Expected exception since mkfs.<format> can not format /dev/null. But that means our .format() function supported it.
						break

					# When we've selected all three criteria,
					# We can safely mark the partition for formatting and where to mount it.
					# TODO: allow_formatting might be redundant since target_mountpoint should only be
					#       set if we actually want to format it anyway.
					mountpoints_set.append(mountpoint)
					partition.allow_formatting = True
					partition.target_mountpoint = mountpoint
					# Only overwrite the filesystem definition if we selected one:
					if len(new_filesystem):
						partition.filesystem = new_filesystem

			archinstall.log('Using existing partition table reported above.')
		elif option == 'format-all':
			if not archinstall.arguments.get('filesystem', None):
				archinstall.arguments['filesystem'] = filesystem_format
			archinstall.arguments['harddrive'].keep_partitions = False
	elif archinstall.arguments['harddrive']:
		# If the drive doesn't have any partitions, safely mark the disk with keep_partitions = False
		# and ask the user for a root filesystem.
		if not archinstall.arguments.get('filesystem', None):
			archinstall.arguments['filesystem'] = filesystem_format
		archinstall.arguments['harddrive'].keep_partitions = False

	# Get disk encryption password (or skip if blank)
	if archinstall.arguments['harddrive'] and archinstall.arguments.get('!encryption-password', None) is None:
		if passwd := encryption_password:
			archinstall.arguments['!encryption-password'] = passwd
			archinstall.arguments['harddrive'].encryption_password = archinstall.arguments['!encryption-password']
	archinstall.arguments["bootloader"] = bootloader
	archinstall.log(f'Bootloader: ' + archinstall.arguments["bootloader"], fg='yellow')
	# Get the hostname for the machine
	if not archinstall.arguments.get('hostname', None):
		archinstall.arguments['hostname'] = input('Desired hostname for the installation: ').strip(' ')
		archinstall.log(f'Hostname: ' + archinstall.arguments["hostname"], fg='yellow')

	# Ask for a root password (optional, but triggers requirement for super-user if skipped)
	if not archinstall.arguments.get('!root-password', None):
		archinstall.arguments['!root-password'] = root_password

	# Ask for additional users (super-user if root pw was not set)
	archinstall.arguments['users'] = {}
	archinstall.arguments['superusers'] = {}
	if not archinstall.arguments.get('!root-password', None):
		archinstall.arguments['superusers'] = {username: {'!password': get_password(prompt=f'Password for user {username}: ')}}

	users, superusers = ({}, {})
	archinstall.arguments['users'] = users
	archinstall.arguments['superusers'] = {**archinstall.arguments['superusers'], **superusers}

	archinstall.log(f'Profile: ' + profile, fg='yellow')

	# Ask for archinstall-specific profiles (such as desktop environments etc)
	if not archinstall.arguments.get('profile', None):
		archinstall.prof
		archinstall.arguments['profile'] = Profile(installer=None, path=profile)
		### TODO: THIS IS WHERE YOU LEFT OFF. THE NEXT THING YOU WERE GONNA DO IS MAKE A CUSTOM PROFILE FOR INSTALLATION ###
	else:
		archinstall.arguments['profile'] = Profile(installer=None, path=archinstall.arguments['profile'])

	# Check the potentially selected profiles preparations to get early checks if some additional questions are needed.
	if archinstall.arguments['profile'] and archinstall.arguments['profile'].has_prep_function():
		with archinstall.arguments['profile'].load_instructions(namespace=f"{archinstall.arguments['profile'].namespace}.py") as imported:
			if not imported._prep_function():
				archinstall.log(' * Profile\'s preparation requirements was not fulfilled.', fg='red')
				exit(1)

	# Ask about audio server selection if one is not already set
	if not archinstall.arguments.get('audio', None):
		# only ask for audio server selection on a desktop profile
		if str(archinstall.arguments['profile']) == 'Profile(desktop)':
			archinstall.arguments['audio'] = archinstall.ask_for_audio_selection()
		else:
			# packages installed by a profile may depend on audio and something may get installed anyways, not much we can do about that.
			# we will not try to remove packages post-installation to not have audio, as that may cause multiple issues
			archinstall.arguments['audio'] = None

	# Ask for preferred kernel:
	if not archinstall.arguments.get("kernels", None):
		kernels = ["linux", "linux-lts", "linux-zen", "linux-hardened"]
		archinstall.arguments['kernels'] = archinstall.select_kernel(kernels)

	# Additional packages (with some light weight error handling for invalid package names)
	print("Only packages such as base, base-devel, linux, linux-firmware, efibootmgr and optional profile packages are installed.")
	print("If you desire a web browser, such as firefox or chromium, you may specify it in the following prompt.")
	while True:
		if not archinstall.arguments.get('packages', None):
			archinstall.arguments['packages'] = [package for package in input('Write additional packages to install (space separated, leave blank to skip): ').split(' ') if len(package)]

		if len(archinstall.arguments['packages']):
			# Verify packages that were given
			try:
				archinstall.log("Verifying that additional packages exist (this might take a few seconds)")
				archinstall.validate_package_list(archinstall.arguments['packages'])
				break
			except archinstall.RequirementError as e:
				archinstall.log(e, fg='red')
				archinstall.arguments['packages'] = None  # Clear the packages to trigger a new input question
		else:
			# no additional packages were selected, which we'll allow
			break

	# Ask or Call the helper function that asks the user to optionally configure a network.
	if not archinstall.arguments.get('nic', None):
		archinstall.arguments['nic'] = archinstall.ask_to_configure_network()
		if not archinstall.arguments['nic']:
			archinstall.log("No network configuration was selected. Network is going to be unavailable until configured manually!", fg="yellow")

	if not archinstall.arguments.get('timezone', None):
		archinstall.arguments['timezone'] = archinstall.ask_for_a_timezone()

	if archinstall.arguments['timezone']:
		if not archinstall.arguments.get('ntp', False):
			archinstall.arguments['ntp'] = input("Would you like to use automatic time synchronization (NTP) with the default time servers? [Y/n]: ").strip().lower() in ('y', 'yes', '')
			if archinstall.arguments['ntp']:
				archinstall.log("Hardware time and other post-configuration steps might be required in order for NTP to work. For more information, please check the Arch wiki.", fg="yellow")


def perform_installation_steps():
	print()
	print('This is your chosen configuration:')
	archinstall.log("-- Guided template chosen (with below config) --", level=logging.DEBUG)
	user_configuration = json.dumps(archinstall.arguments, indent=4, sort_keys=True, cls=archinstall.JSON)
	archinstall.log(user_configuration, level=logging.INFO)
	with open("/var/log/archinstall/user_configuration.json", "w") as config_file:
		config_file.write(user_configuration)
	print()

	if not archinstall.arguments.get('silent'):
		input('Press Enter to continue.')

	"""
		Issue a final warning before we continue with something un-revertable.
		We mention the drive one last time, and count from 5 to 0.
	"""

	if archinstall.arguments.get('harddrive', None):
		print(f" ! Formatting {archinstall.arguments['harddrive']} in ", end='')
		archinstall.do_countdown()

		"""
			Setup the blockdevice, filesystem (and optionally encryption).
			Once that's done, we'll hand over to perform_installation()
		"""
		mode = archinstall.GPT
		if has_uefi() is False:
			mode = archinstall.MBR
		with archinstall.Filesystem(archinstall.arguments['harddrive'], mode) as fs:
			# Wipe the entire drive if the disk flag `keep_partitions`is False.
			if archinstall.arguments['harddrive'].keep_partitions is False:
				fs.use_entire_disk(root_filesystem_type=archinstall.arguments.get('filesystem', 'btrfs'))

			# Check if encryption is desired and mark the root partition as encrypted.
			if archinstall.arguments.get('!encryption-password', None):
				root_partition = fs.find_partition('/')
				root_partition.encrypted = True

			# After the disk is ready, iterate the partitions and check
			# which ones are safe to format, and format those.
			for partition in archinstall.arguments['harddrive']:
				if partition.safe_to_format():
					# Partition might be marked as encrypted due to the filesystem type crypt_LUKS
					# But we might have omitted the encryption password question to skip encryption.
					# In which case partition.encrypted will be true, but passwd will be false.
					if partition.encrypted and (passwd := archinstall.arguments.get('!encryption-password', None)):
						partition.encrypt(password=passwd)
					else:
						partition.format()
				else:
					archinstall.log(f"Did not format {partition} because .safe_to_format() returned False or .allow_formatting was False.", level=logging.DEBUG)

			if archinstall.arguments.get('!encryption-password', None):
				# First encrypt and unlock, then format the desired partition inside the encrypted part.
				# archinstall.luks2() encrypts the partition when entering the with context manager, and
				# unlocks the drive so that it can be used as a normal block-device within archinstall.
				with archinstall.luks2(fs.find_partition('/'), 'luksloop', archinstall.arguments.get('!encryption-password', None)) as unlocked_device:
					unlocked_device.format(fs.find_partition('/').filesystem)
					unlocked_device.mount(archinstall.storage.get('MOUNT_POINT', '/mnt'))
			else:
				fs.find_partition('/').mount(archinstall.storage.get('MOUNT_POINT', '/mnt'))

			if has_uefi():
				fs.find_partition('/boot').mount(archinstall.storage.get('MOUNT_POINT', '/mnt') + '/boot')

	perform_installation(archinstall.storage.get('MOUNT_POINT', '/mnt'))


def perform_installation(mountpoint):
	"""
	Performs the installation steps on a block device.
	Only requirement is that the block devices are
	formatted and setup prior to entering this function.
	"""
	with archinstall.Installer(mountpoint, kernels=archinstall.arguments.get('kernels', 'linux')) as installation:
		# if len(mirrors):
		# Certain services might be running that affects the system during installation.
		# Currently, only one such service is "reflector.service" which updates /etc/pacman.d/mirrorlist
		# We need to wait for it before we continue since we opted in to use a custom mirror/region.
		installation.log('Waiting for automatic mirror selection (reflector) to complete.', level=logging.INFO)
		while archinstall.service_state('reflector') not in ('dead', 'failed'):
			time.sleep(1)
		# Set mirrors used by pacstrap (outside of installation)
		if archinstall.arguments.get('mirror-region', None):
			archinstall.use_mirrors(archinstall.arguments['mirror-region'])  # Set the mirrors for the live medium
		if installation.minimal_installation():
			installation.set_locale(archinstall.arguments['sys-language'], archinstall.arguments['sys-encoding'].upper())
			installation.set_hostname(archinstall.arguments['hostname'])
			if archinstall.arguments['mirror-region'].get("mirrors", None) is not None:
				installation.set_mirrors(archinstall.arguments['mirror-region'])  # Set the mirrors in the installation medium
			if archinstall.arguments["bootloader"] == "grub-install" and has_uefi():
				installation.add_additional_packages("grub")
			installation.add_bootloader(archinstall.arguments["bootloader"])

			# If user selected to copy the current ISO network configuration
			# Perform a copy of the config
			if archinstall.arguments.get('nic', {}) == 'Copy ISO network configuration to installation':
				installation.copy_iso_network_config(enable_services=True)  # Sources the ISO network configuration to the install medium.
			elif archinstall.arguments.get('nic', {}).get('NetworkManager', False):
				installation.add_additional_packages("networkmanager")
				installation.enable_service('NetworkManager.service')
			# Otherwise, if a interface was selected, configure that interface
			elif archinstall.arguments.get('nic', {}):
				installation.configure_nic(**archinstall.arguments.get('nic', {}))
				installation.enable_service('systemd-networkd')
				installation.enable_service('systemd-resolved')

			if archinstall.arguments.get('audio', None) is not None:
				installation.log(f"This audio server will be used: {archinstall.arguments.get('audio', None)}", level=logging.INFO)
				if archinstall.arguments.get('audio', None) == 'pipewire':
					print('Installing pipewire ...')

					installation.add_additional_packages(["pipewire", "pipewire-alsa", "pipewire-jack", "pipewire-media-session", "pipewire-pulse", "gst-plugin-pipewire", "libpulse"])
				elif archinstall.arguments.get('audio', None) == 'pulseaudio':
					print('Installing pulseaudio ...')
					installation.add_additional_packages("pulseaudio")
			else:
				installation.log("No audio server will be installed.", level=logging.INFO)
				
			# Enabling multilib repository
			enable_multilib(installation)

			# Enabling pacman color
			replace_in_file(installation, '/etc/pacman.conf', '#Color', 'Color')

			# Enabling pacman parallel downloads
			replace_in_file(installation, '/etc/pacman.conf', '#ParallelDownloads = 5', 'ParallelDownloads = 5')

			# Increasing makeflags in makepkg.conf
			set_makeflags(installation)

			if archinstall.arguments.get('packages', None) and archinstall.arguments.get('packages', None)[0] != '':
				installation.add_additional_packages(archinstall.arguments.get('packages', None))

			if archinstall.arguments.get('profile', None):
				installation.install_profile(archinstall.arguments.get('profile', None))

			for user, user_info in archinstall.arguments.get('users', {}).items():
				installation.user_create(user, user_info["!password"], sudo=False)

			for superuser, user_info in archinstall.arguments.get('superusers', {}).items():
				installation.user_create(superuser, user_info["!password"], sudo=False)
				with open(f'{installation.target}/etc/sudoers', 'a') as sudoers:
					sudoers.write(f'{superuser} ALL=(ALL) NOPASSWD: ALL\n')
				installation.helper_flags['user'] = True

			if timezone := archinstall.arguments.get('timezone', None):
				installation.set_timezone(timezone)

			if archinstall.arguments.get('ntp', False):
				installation.activate_ntp()

			if (root_pw := archinstall.arguments.get('!root-password', None)) and len(root_pw):
				installation.user_set_pw('root', root_pw)

			# This step must be after profile installs to allow profiles to install language pre-requisits.
			# After which, this step will set the language both for console and x11 if x11 was installed for instance.
			installation.set_keyboard_language(archinstall.arguments['keyboard-language'])

			if archinstall.arguments['profile'] and archinstall.arguments['profile'].has_post_install():
				with archinstall.arguments['profile'].load_instructions(namespace=f"{archinstall.arguments['profile'].namespace}.py") as imported:
					if not imported._post_install():
						archinstall.log(' * Profile\'s post configuration requirements was not fulfilled.', fg='red')
						exit(1)

		# If the user provided a list of services to be enabled, pass the list to the enable_service function.
		# Note that while it's called enable_service, it can actually take a list of services and iterate it.
		if archinstall.arguments.get('services', None):
			installation.enable_service(*archinstall.arguments['services'])

		# Display warning message when no AUR helper specified.
		if archinstall.arguments.get('aur-packages', None) and not archinstall.arguments.get('aur-helper', None):
			archinstall.log(f"No AUR helper specified. No AUR packages will be installed. Add 'aur-helper' to the config")

		# If the user provided an AUR helper to be installed, install it now.
		# In addition, install user-defined AUR packages, if they exist.
		if archinstall.arguments.get('aur-helper', None):
			install_aur_helper(archinstall.arguments['aur-helper'], installation)
			if archinstall.arguments.get('aur-packages', None):
				install_aur_packages(installation, archinstall.arguments['aur-packages'])

		# If the user provided custom commands to be run post-installation, execute them now.
		if archinstall.arguments.get('custom-commands', None):
			run_custom_user_commands(archinstall.arguments['custom-commands'], installation)

		installation.log("For post-installation tips, see https://wiki.archlinux.org/index.php/Installation_guide#Post-installation", fg="yellow")
		if not archinstall.arguments.get('silent'):
			choice = input("Would you like to chroot into the newly created installation and perform post-installation configuration? [Y/n] ")
			if choice.lower() in ("y", ""):
				try:
					installation.drop_to_shell()
				except:
					pass

	# For support reasons, we'll log the disk layout post installation (crash or no crash)
	archinstall.log(f"Disk states after installing: {archinstall.disk_layouts()}", level=logging.DEBUG)

def enable_multilib(installation: Installer):
	replace_in_file(installation, '/etc/pacman.conf', '#[multilib]\n#Include = /etc/pacman.d/mirrorlist', '[multilib]\nInclude = /etc/pacman.d/mirrorlist')

def set_makeflags(installation: Installer, makeflags='-j$(nproc)'):
	replace_in_file(installation, '/etc/makepkg.conf', '#MAKEFLAGS="-j2"', f'MAKEFLAGS="{makeflags}"')

def replace_in_file(installation: Installer, path: str, before: str, after: str):
	with open(f'{installation.target}{path}', 'r') as file:
		filedata = file.read()

	filedata = filedata.replace(before, after)

	with open(f'{installation.target}{path}', 'w') as file:
		file.write(filedata)

def install_aur_helper(helper_name: str, installation: Installer):
	archinstall.log(f"Installing {helper_name}...")
	user = list(archinstall.arguments.get('superusers', {}).keys())[0]
	installation.add_additional_packages(['git'])
	arch_chroot(installation, f'git clone https://aur.archlinux.org/{helper_name}.git /home/{user}/{helper_name}', runas=user)
	arch_chroot(installation, f'cd /home/{user}/{helper_name} && makepkg -si --noconfirm', runas=user)
	# installation.arch_chroot(f'rm -rf /home/{user}/{helper_name}', runas=user)
	arch_chroot(installation, f'/usr/bin/{helper_name} --save --nocleanmenu --nodiffmenu --noeditmenu --removemake', runas=user)

def arch_chroot(installation, cmd, *args, **kwargs):
	if 'runas' in kwargs:
		cmd = f"su - {kwargs['runas']} -c \"{cmd}\""

	return SysCommand(f'/usr/bin/arch-chroot {installation.target} {cmd}', peak_output=True)

def install_aur_packages(installation: Installer, *packages, **kwargs):
	if type(packages[0]) in (list, tuple):
		packages = packages[0]
	archinstall.log(f'Installing packages: {packages}', level=logging.INFO)

	user = list(archinstall.arguments.get('superusers', {}).keys())[0]

	if (sync_mirrors := SysCommand('/usr/bin/pacman -Syy')).exit_code == 0:
		for package in packages:
			archinstall.log(f'Installing package: {package}', level=logging.INFO)
			if (pacstrap := arch_chroot(installation, f'/usr/bin/{archinstall.arguments["aur-helper"]} -S --noconfirm {package}', runas=user)).exit_code == 0:
				archinstall.log(f'Installed {package}', level=logging.INFO)
			else:
				archinstall.log(f'Could not install packages: {pacstrap.exit_code}', level=logging.INFO)
	else:
		archinstall.log(f'Could not sync mirrors: {sync_mirrors.exit_code}', level=logging.INFO)


if not check_mirror_reachable():
	log_file = os.path.join(archinstall.storage.get('LOG_PATH', None), archinstall.storage.get('LOG_FILE', None))
	archinstall.log(f"Arch Linux mirrors are not reachable. Please check your internet connection and the log file '{log_file}'.", level=logging.INFO, fg="red")
	exit(1)

if archinstall.arguments.get('silent', None) is None:
	ask_user_questions()
# else:
# 	# Workarounds if config is loaded from a file
# 	# The harddrive section should be moved to perform_installation_steps, where it's actually being performed
# 	# Blockdevice object should be created in perform_installation_steps
# 	# This needs to be done until then
# 	archinstall.arguments['harddrive'] = archinstall.BlockDevice(path=archinstall.arguments['harddrive']['path'])
# 	# Temporarily disabling keep_partitions if config file is loaded
# 	archinstall.arguments['harddrive'].keep_partitions = False
# 	# Temporary workaround to make Desktop Environments work
# 	if archinstall.arguments.get('profile', None) is not None:
# 		if type(archinstall.arguments.get('profile', None)) is dict:
# 			archinstall.arguments['profile'] = archinstall.Profile(None, archinstall.arguments.get('profile', None)['path'])
# 		else:
# 			archinstall.arguments['profile'] = archinstall.Profile(None, archinstall.arguments.get('profile', None))
# 	else:
# 		archinstall.arguments['profile'] = None
# 	if archinstall.arguments.get('mirror-region', None) is not None:
# 		if type(archinstall.arguments.get('mirror-region', None)) is dict:
# 			archinstall.arguments['mirror-region'] = archinstall.arguments.get('mirror-region', None)
# 		else:
# 			selected_region = archinstall.arguments.get('mirror-region', None)
# 			archinstall.arguments['mirror-region'] = {selected_region: archinstall.list_mirrors()[selected_region]}
# 	archinstall.arguments['sys-language'] = archinstall.arguments.get('sys-language', 'en_US')
# 	archinstall.arguments['sys-encoding'] = archinstall.arguments.get('sys-encoding', 'utf-8')
# 	if archinstall.arguments.get('gfx_driver', None) is not None:
# 		archinstall.storage['gfx_driver_packages'] = AVAILABLE_GFX_DRIVERS.get(archinstall.arguments.get('gfx_driver', None), None)

ask_user_questions()
perform_installation_steps()