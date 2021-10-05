#!/bin/sh

rm -rf ./testimage.img
losetup -D
umount -a
dd if=/dev/zero of=./testimage.img bs=1G count=30
losetup -fP ./testimage.img
losetup -a | grep "testimage.img" | awk -F ":" '{print $1}'
python install.py --config ./desktop.json --var
qemu-system-x86_64 -enable-kvm -machine q35,accel=kvm -device intel-iommu -cpu host -m 4096 -boot order=d -drive file=./testimage.img,format=raw -drive if=pflash,format=raw,readonly,file=/usr/share/ovmf/x64/OVMF_CODE.fd -drive if=pflash,format=raw,readonly,file=/usr/share/ovmf/x64/OVMF_VARS.fd
