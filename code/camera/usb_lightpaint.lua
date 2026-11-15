-- usb_lightpaint.lua  CHDK light painting script with USB remote control.
-- Copy to CHDK/SCRIPTS/ on the SD card.
--
-- Camera setup before running:
--   Mode dial: M
--   Focus: MF (manual focus)
--   Flash: off (script forces this via set_prop)
--   Review time: 0
--   CHDK > Enhanced Photo Operations > Override Tv: enabled
--   CHDK > Enhanced Photo Operations > Override subj. dist.: enabled
--
-- Pulse encoding (must match usb_remote ESP32 sketch):
--   1 pulse       shoot with current Tv and dist
--   2..9          set Tv index 1..8   (1s..120s)
--   10..13        set dist index 1..4 (0.5m..3m)

-- TV_N / DIST_N must equal the table lengths in the ESP32 sketch.
local TV_N   = 8
local DIST_N = 4

-- tv96 = -96 * log2(seconds). Matches tv_labels in ESP32 sketch.
local tv_table = {0, -96, -192, -288, -375, -471, -567, -663}
--                1s   2s    4s   10s   17s   30s   60s  120s

-- Focus distances in mm. Matches dist_labels in ESP32 sketch.
local dist_table = {410, 1000, 2000, 3000}
--                 410mm    1m    2m    3m

local tv_sel   = 0
local dist_sel = 0

set_prop(16, 2)  -- force flash off
print("ready  tv=" .. tv_sel .. " dist=" .. dist_sel)

while true do

    -- wait for first pulse of a burst
    wait_click(10000)

    if is_key("remote") then

        -- count remaining pulses; 700ms silence signals end of burst
        local count = 1
        while true do
            wait_click(700)
            if is_key("remote") then
                count = count + 1
            else
                break
            end
        end

        sleep(200)  -- debounce: flush any trailing events before acting
        if count == 1 then
            if tv_sel > 0 then set_tv96_direct(tv_table[tv_sel]) end
            if dist_sel > 0 then set_focus(dist_table[dist_sel]) end
            print("shoot tv=" .. tv_sel .. " dist=" .. dist_sel)
            shoot()
            print("ready")

        elseif count >= 2 and count <= TV_N + 1 then
            tv_sel = count - 1
            print("tv=" .. tv_sel)

        elseif count >= TV_N + 2 and count <= TV_N + DIST_N + 1 then
            dist_sel = count - TV_N - 1
            print("dist=" .. dist_sel)

        elseif count == TV_N + DIST_N + 2 then
            tv_sel = 0
            print("tv=off")

        elseif count == TV_N + DIST_N + 3 then
            dist_sel = 0
            print("dist=off")

        else
            print("?? " .. count)
        end

    end

end
