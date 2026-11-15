-- usb_bulb.lua: USB remote bulb control for CHDK.
-- Copy to CHDK/SCRIPTS/ on the SD card.
-- Before running: M mode, flash Off, review time 0.
-- Pulse USB power once to open shutter, again to close.

print("waiting for pulse")

while get_usb_power() == 1 do sleep(10) end   -- ensure starting low

while get_usb_power() == 0 do sleep(10) end   -- wait for first pulse
press("shoot_full")
print("shutter open")
while get_usb_power() == 1 do sleep(10) end   -- wait for pulse end

while get_usb_power() == 0 do sleep(10) end   -- wait for second pulse
release("shoot_full")
print("shutter closed")
while get_usb_power() == 1 do sleep(10) end

print("done")
