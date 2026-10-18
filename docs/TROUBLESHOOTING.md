# Pi / RTL-SDR troubleshooting

1. Test `lsusb`; identify Realtek RTL2832U device.
2. Run `rtl_test` and stop with Ctrl+C. Never run it simultaneously with capture.
3. If you see `usb_claim_interface error -6`, check if a DVB kernel driver owns the
   interface: `lsmod | grep rtl28`. On the Pi, try (temporary until reboot):
   `sudo modprobe -r dvb_usb_rtl28xxu` and retry `rtl_test`.
4. If input overflows / lost samples: USB hub or power supply may be the problem.
   Pi 4 needs a reliable USB-C 5V/3A supply; also check `vcgencmd get_throttled`.
5. A correct 20MB capture does **not** imply valid aircraft data in that capture.
   Check antenna position and real decoder output before blaming DSP.
6. `rtl_test -t` saying `No E4000 tuner found` is normal when tuner is R820T.
7. On changing sampling rate (2.0 vs 2.4 MS/s), time-domain indices and preamble
   sample offsets change. The provided synthetic dataset is SPECIFIC to 2.0 MS/s.
