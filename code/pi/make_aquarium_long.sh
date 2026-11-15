#!/usr/bin/env bash
#
# make_aquarium_long.sh dir — atelier's long aquarium video, dir/aquarium_long.mp4:
# the clips listed in pi/aquarium_clips.tsv (name, URL, licence, credit; in the
# order played) downloaded into dir, each brought to 1920x1080 30 fps, then
# joined with crossfades: H.264, no sound, about 25 minutes, ~1.7 GB. Also
# writes dir/aquarium_credits.txt (the CC BY / BY-SA clips ask for a credit).
#
# Sources: Mixkit clips free at 1080p (the seven in make_aquarium1080.sh and
# 4133 dolphins), and Wikimedia Commons clips at 1080p (Commons' own 1080p
# VP9 transcodes: the originals run to a gigabyte a minute). Commons
# rate-limits, so downloads go one at a time with a pause.
#
# Too big for atelier's RAM overlay: install it into the read-only layer only
# (see pi/README.md, "atelier").

set -e
dir=${1:?usage: make_aquarium_long.sh dir}
list=$(dirname "$0")/aquarium_clips.tsv
F=1.5                                # crossfade seconds
UA="atelier-aquarium/1.0"

mkdir -p "$dir/norm"
names=()
while IFS=$'\t' read -r name url lic credit; do
    [[ -z $name || $name == \#* ]] && continue
    names+=("$name")
    src=$(ls "$dir/$name".* 2>/dev/null | grep -v '\.part$' | head -1 || true)
    if [ -z "$src" ]; then
        ext=${url##*.}; src="$dir/$name.$ext"
        for a in 1 2 3; do
            curl -sfL -A "$UA" -o "$src.part" "$url" && mv "$src.part" "$src" && break
            echo "retry $name" >&2; sleep 20
        done
        [ -s "$src" ] || { echo "cannot download $name" >&2; exit 1; }
        sleep 3
    fi
    out="$dir/norm/$name.mp4"
    [ -s "$out" ] || ffmpeg -nostdin -v error -y -i "$src" -an \
        -vf "scale=1920:1080,fps=30,format=yuv420p,setsar=1" \
        -c:v libx264 -preset fast -crf 14 "$out"
    echo "$name"
done < "$list"

grep -v '^#' "$list" | awk -F'\t' 'NF {print $4 " (" $3 ")"}' > "$dir/aquarium_credits.txt"

in=(); fc=""; i=0; off=0; prev=""
for n in "${names[@]}"; do
    in+=(-i "$dir/norm/$n.mp4")
    d=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of csv=p=0 "$dir/norm/$n.mp4")
    fc+="[$i:v]settb=AVTB[v$i];"
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
    -maxrate 10M -bufsize 20M -g 60 -movflags +faststart "$dir/aquarium_long.mp4"
echo "$dir/aquarium_long.mp4"
