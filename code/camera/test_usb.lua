print("start")
  local last = -1
  while true do
      local v = get_usb_power(1)
      if v ~= last then
          print("usb: "..v)
          last = v
      end
      sleep(10)
  end
