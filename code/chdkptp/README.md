# chdkptp

The Canon A570 IS was controlled from the computer over USB by
[chdkptp](https://app.assembla.com/spaces/chdkptp/wiki), which talks to
[CHDK](https://chdk.fandom.com/wiki/CHDK) running on the camera. CHDK
itself needed no changes. chdkptp needed two kinds, in
[`chdkptp_macos_long_exposure.patch`](chdkptp_macos_long_exposure.patch).

## Building on a current Mac

The source is the 2013 snapshot on GitHub,
[simonswine/chdkptp](https://github.com/simonswine/chdkptp) (commit
`e41ce78`), as the original Subversion server no longer allows
anonymous access. It was written for Lua 5.1, and builds against Lua
5.2.4 with three small changes:

- `include.mk`: `-DLUA_COMPAT_ALL -DLUA_COMPAT_APIINTCASTS` in CFLAGS.
- `lfs/lfs.c`: `struct luaL_reg` becomes `luaL_Reg`.
- `chdkptp.c`: `lua_open()` becomes `luaL_newstate()`.

It also needs libusb-compat (`brew install libusb libusb-compat`) for
the old libusb-0.1 interface, and Lua 5.2.4 built from source, since
Homebrew only has 5.4. The build was command-line only, without the
GUI.

## Long exposures

A light painting keeps the shutter open for the whole plot, often
several minutes, and as it came chdkptp gave up on any shot longer
than about 20 seconds. Three timeouts are raised to 15 minutes:

- `lua/cli.lua`: the camera-side capture timeout that `remoteshoot`
  sets (`opts.cap_timeout`), and its wait for the camera's status.
- `lua/chdku.lua`: the wait for the picture's data in
  `capture_get_data`.

To apply the patch, from inside the chdkptp source:
`git apply chdkptp_macos_long_exposure.patch`.

chdkptp is under the GPL v2, and so is this patch.
