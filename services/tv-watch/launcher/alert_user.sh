#!/bin/bash
# Play attention ping when the TV screen needs to be read by the user
for i in 1 2 3; do
  canberra-gtk-play -i bell 2>/dev/null \
    || paplay /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null \
    || aplay /usr/share/sounds/freedesktop/stereo/bell.wav 2>/dev/null
  sleep 0.35
done
