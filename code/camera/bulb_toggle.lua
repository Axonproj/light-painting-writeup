-- bulb_toggle.lua: set long exposure beyond camera's 15s limit, shoot on half-press.
-- Copy to CHDK/SCRIPTS/ on the SD card.
-- Before running: M mode, flash Off, review time 0.
-- Re-run the script for each shot.

-- TV96 = -96 * log2(seconds). Pick one:
--   30s = -471,  60s = -567,  90s = -621, 120s = -663
--  180s = -721, 240s = -759, 300s = -790, 600s = -886
local TV96 = -471

set_tv96_direct(TV96)
print("tv set - half-press to shoot")

while true do
    if is_key("shoot_half") then
        print("shooting")
        press("shoot_full")
        sleep(500)
        release("shoot_full")
        break
    end
    sleep(20)
end
