#!/usr/bin/env bash
#
# make_aquarium1080.sh dir — atelier's aquarium video, dir/aquarium1080.mp4:
# free Mixkit clips (mixkit.co, Mixkit Stock Video Free License) that are free
# at 1080p, downloaded into dir and joined with crossfades, 1920x1080 30 fps
# H.264, no sound, about 2.5 minutes. Only 4021 is an aquarium; the rest are
# reefs in the sea (the Roku's aquarium clips are free at 720p only).
# Clips are Mixkit ids, in the order played.
#
#   4021 moss tank, piranhas   4418 sunlit reef     4291 shoal in blue water
#   4406 nurse shark           4419 fish on a rock  4465 turtle grazing
#   4020 coral

set -e
dir=${1:?usage: make_aquarium1080.sh dir}
CLIPS=(4021 4418 4291 4406 4419 4465 4020)
F=1.5                                # crossfade seconds

mkdir -p "$dir"
in=(); fc=""; i=0; off=0; prev=""
for c in "${CLIPS[@]}"; do
    [ -s "$dir/$c.mp4" ] || curl -sfo "$dir/$c.mp4" "https://assets.mixkit.co/videos/$c/$c-1080.mp4"
    in+=(-i "$dir/$c.mp4")
    d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$dir/$c.mp4")
    fc+="[$i:v]scale=1920:1080,fps=30,format=yuv420p,setsar=1,settb=AVTB[v$i];"
    if [ $i -eq 0 ]; then
        prev=v0; off=$d
    else
        off=$(echo "$off - $F" | bc)
        fc+="[$prev][v$i]xfade=transition=fade:duration=$F:offset=$off[x$i];"
        prev=x$i; off=$(echo "$off + $d" | bc)
    fi
    i=$((i+1))
done
# Level 4.1 and a 10 Mbit/s ceiling: well inside the Pi 3's hardware decoder.
ffmpeg -v error -y "${in[@]}" -filter_complex "${fc%;}" -map "[$prev]" -an -pix_fmt yuv420p \
    -c:v libx264 -profile:v high -level 4.1 -preset slow -crf 22 \
    -maxrate 10M -bufsize 20M -g 60 -movflags +faststart "$dir/aquarium1080.mp4"
echo "$dir/aquarium1080.mp4"
