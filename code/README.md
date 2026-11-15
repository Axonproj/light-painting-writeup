# Code

These are the programs behind the pictures and the process
described in the write-up. Claude wrote them, to descriptions of what
each picture should be.

They were written for one particular rig: a Tektronix 4662 running
GRBL, a wireless LED on the carriage, a camera over the bed and a Perl
script that sends plot files to the plotter. So they won't run a
different setup as they stand, but the ideas should carry over. Each
file starts with a description of what it does and how.

They need Python 3 with numpy and Pillow, and `raw_paint.py` also needs
rawpy. Run any of them with `--help` for its options. A generator
writes a `.plot` file and a preview image, into `plots/` and
`previews/` beside this folder unless `--out` names somewhere else.

## Pictures

| Program | Picture | What it draws |
|---|---|---|
| [`no_exit.py`](no_exit.py) | No Exit | Snakes on a grid looking for a way out of a box that has none |
| [`sphere.py`](sphere.py) | Phong | A Phong-shaded sphere in horizontal lines |
| [`teapot.py`](teapot.py) | Teapot | The Utah teapot, from Martin Newell's patch data, shaded in horizontal lines with its shadow |
| [`torus3d.py`](torus3d.py) | Torus | A saddle-shaped torus drawn as shaded strands over its shadow |
| [`rings3d.py`](rings3d.py) | Orbits | Rainbow rings tipped back in perspective, lit as glowing tubes |
| [`sunset.py`](sunset.py) | Portal | A red sun against a blue sky in vertical lines |
| [`cube_anim.py`](cube_anim.py) | Cube | A wireframe cube turning, one frame per light painting |

## The process

| Program | What it does |
|---|---|
| [`cal_colour.py`](cal_colour.py) | The calibration grid, every line its own colour, and its reference image |
| [`calmap.py`](calmap.py) | Fits the map from the photographed grid to the bed, which removes the lens distortion and perspective |
| [`raw_paint.py`](raw_paint.py) | Develops the camera's raw files, subtracts the dark frame, repairs hot pixels and applies the map |
| [`ed_cal.py`](ed_cal.py) | An energy-density wedge in one colour |
| [`hue_ed.py`](hue_ed.py) | The hue map: each colour at a range of energy densities |
| [`annotate.py`](annotate.py) | Adds lettering to a finished picture from `#@` lines in its plot file |

## Running the plotter

[`plotter.pl`](plotter.pl) is the Perl script that drives everything. It
talks to the plotter's WiFi bridge, turns a small command language
(`home`, `moveto`, `drawto`, `colour`, `speed` and so on) into G-code
for GRBL, sets the LED's colours, and runs whole plot files. Its
`plotsnap` and `paint` commands also take the photographs, switch the
room lights and call the Python above to make the finished picture.
It is the most tied to this rig of anything here: the bridge, the
camera on a Raspberry Pi and the lights controller all have fixed
addresses on the local network. It needs Perl with XML::LibXML, and
uses two modules of its own: [`PlotterSVG.pm`](PlotterSVG.pm) turns SVG
drawings into plotter moves, and [`HersheyFont.pm`](HersheyFont.pm)
draws lettering in the Hershey single-stroke fonts (public domain, from
A. V. Hershey, 1967), whose data is in `simplex.txt`, `scriptc.txt` and
`scripts.txt`.

The [camera](camera/) folder has `remote_snap`, which `plotter.pl`
calls to take each photograph, and the scripts that ran on the A570
itself before chdkptp.

The [pi](pi/) folder has what runs on the two Raspberry Pis: the
camera host by the plotter (its setup is in the README there), and a
second Pi that shows an aquarium video on the room's monitor, and the
newest pictures while someone is in the room, and goes dark for every
exposure. `display_sync.py` is the computer's side of that.

The rest are helpers the Python programs use: `outdirs.py`,
`photo_hlines.py`, `rate_test.py`, `harmonograph3d.py`, `picmeta.py`
and `undistort.py`.

## Firmware

The [firmware](firmware/) folder has the code for the plotter's three
microcontrollers: the WiFi bridge, the LED pen carrier, and the
changes to GRBL on the Arduino Nano. The [chdkptp](chdkptp/) folder has
the changes that let the computer control the Canon A570 through long
exposures.

The code is under the MIT licence, apart from the two patches, which
take their programs' licences: GPL v3 for GRBL and GPL v2 for chdkptp.
See [LICENSE](LICENSE).
