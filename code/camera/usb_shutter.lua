-- usb_shutter.lua: USB remote shutter trigger for CHDK.
-- Copy to CHDK/SCRIPTS/ on the SD card.
-- Before running: M mode, 15s TV, flash Off, review time 0.
-- Each USB pulse triggers one exposure.

set_config_value(121, 1)   -- enable USB remote
print("ready")

while true do
    wait_click(100)
    if is_key("remote") then
        print("shoot")
        press("shoot_full")
        sleep(500)
        release("shoot_full")
        while is_key("remote") do sleep(10) end
        sleep(200)
        print("ready")
    end
end
