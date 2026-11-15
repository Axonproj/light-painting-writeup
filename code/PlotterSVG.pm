package PlotterSVG;
#
# PlotterSVG.pm — Parse SVG files and produce moveto/drawto commands
# for the Tektronix 4662 plotter.
#
# SVG geometry is flattened to line segments, Y-flipped, and returned
# as a list of {cmd => 'moveto'|'drawto', x => N, y => N} hashes
# in plotter paper-space (mm, origin bottom-left, +Y up).
#

use strict;
use warnings;
use XML::LibXML;
use POSIX qw(ceil floor);
use Exporter 'import';

our @EXPORT_OK = qw(load_svg svg_info);

use constant PI => 4 * atan2(1, 1);

my $DEFAULT_TOLERANCE = 0.05;  # mm, bezier flattening tolerance.

# ---- Public API -------------------------------------------------------------

sub load_svg {
  my ($file, %opts) = @_;

  my $mode      = $opts{mode}      // '';       # '' | 'fit' | 'scale'
  my $scale     = $opts{scale}     // 1;
  my $area_w    = $opts{area_w}    // 393;      # available plot width mm
  my $area_h    = $opts{area_h}    // 262;      # available plot height mm
  my $tolerance = $opts{tolerance} // $DEFAULT_TOLERANCE;

  my $doc = XML::LibXML->load_xml(location => $file);
  my $svg = $doc->documentElement();

  my ($svg_w, $svg_h, $vb_matrix) = compute_viewbox_transform($svg);

  my @cmds;
  process_element($svg, $vb_matrix, \@cmds, $tolerance, $doc);

  # Y-flip and scaling only apply to geometry commands (moveto/drawto).
  for my $c (@cmds) {
    next unless exists $c->{x};
    $c->{y} = $svg_h - $c->{y};
  }

  if ($mode eq 'fit' && @cmds) {
    my ($min_x, $min_y, $max_x, $max_y) = bounding_box(\@cmds);
    my $w = $max_x - $min_x;
    my $h = $max_y - $min_y;
    if ($w > 0 && $h > 0) {
      my $sx = $area_w / $w;
      my $sy = $area_h / $h;
      my $s = ($sx < $sy) ? $sx : $sy;
      my $ox = ($area_w - $w * $s) / 2 - $min_x * $s;
      my $oy = ($area_h - $h * $s) / 2 - $min_y * $s;
      for my $c (@cmds) {
        next unless exists $c->{x};
        $c->{x} = $c->{x} * $s + $ox;
        $c->{y} = $c->{y} * $s + $oy;
      }
    }
  } elsif ($mode eq 'scale' && $scale != 1) {
    for my $c (@cmds) {
      next unless exists $c->{x};
      $c->{x} *= $scale;
      $c->{y} *= $scale;
    }
  }

  return \@cmds;
}

sub svg_info {
  my ($file) = @_;

  my $doc = XML::LibXML->load_xml(location => $file);
  my $svg = $doc->documentElement();

  my ($svg_w, $svg_h, $vb_matrix) = compute_viewbox_transform($svg);

  my @cmds;
  process_element($svg, $vb_matrix, \@cmds, $DEFAULT_TOLERANCE, $doc);

  # Y-flip for bounding box.
  for my $c (@cmds) {
    $c->{y} = $svg_h - $c->{y};
  }

  my $moves = grep { $_->{cmd} eq 'moveto' } @cmds;
  my $draws = grep { $_->{cmd} eq 'drawto' } @cmds;

  my %info = (
    file      => $file,
    svg_w     => $svg_w,
    svg_h     => $svg_h,
    commands  => scalar @cmds,
    moveto    => $moves,
    drawto    => $draws,
  );

  if (@cmds) {
    my ($min_x, $min_y, $max_x, $max_y) = bounding_box(\@cmds);
    $info{min_x}  = $min_x;
    $info{min_y}  = $min_y;
    $info{max_x}  = $max_x;
    $info{max_y}  = $max_y;
    $info{width}  = $max_x - $min_x;
    $info{height} = $max_y - $min_y;
  }

  return \%info;
}

# ---- DOM Walker -------------------------------------------------------------

sub process_element {
  my ($elem, $matrix, $cmds, $tol, $doc) = @_;

  for my $child ($elem->childNodes()) {
    next unless $child->nodeType == XML_ELEMENT_NODE;

    my $tag = $child->localname();

    # Skip <defs> — only used for <use> lookup.
    next if $tag eq 'defs';

    # Stack transform.
    my $m = $matrix;
    my $tr = $child->getAttribute('transform');
    if (defined $tr && $tr ne '') {
      $m = matrix_multiply($matrix, parse_transform($tr));
    }

    if ($tag eq 'g' || $tag eq 'svg') {
      process_element($child, $m, $cmds, $tol, $doc);
    } elsif ($tag eq 'use') {
      process_use($child, $m, $cmds, $tol, $doc);
    } else {
      # Emit a colour command before each drawable element if it carries a stroke colour.
      my $col = stroke_colour($child);
      push @$cmds, { cmd => 'colour', colour => $col } if defined $col;

      if ($tag eq 'path') {
        my $d = $child->getAttribute('d');
        process_path($d, $m, $cmds, $tol) if defined $d;
      } elsif ($tag eq 'line') {
        process_line($child, $m, $cmds);
      } elsif ($tag eq 'polyline') {
        process_polyline($child, $m, $cmds, 0);
      } elsif ($tag eq 'polygon') {
        process_polyline($child, $m, $cmds, 1);
      } elsif ($tag eq 'rect') {
        process_rect($child, $m, $cmds, $tol);
      } elsif ($tag eq 'circle') {
        process_circle($child, $m, $cmds, $tol);
      } elsif ($tag eq 'ellipse') {
        process_ellipse($child, $m, $cmds, $tol);
      }
    }
    # Ignore text, image, and other unsupported elements.
  }
}

sub process_use {
  my ($elem, $matrix, $cmds, $tol, $doc) = @_;

  my $href = $elem->getAttribute('xlink:href')
          // $elem->getAttribute('href');
  return unless defined $href;
  $href =~ s/^#//;

  # Find the referenced element by id.
  my @found = $doc->findnodes("//*[\@id='$href']");
  return unless @found;
  my $target = $found[0];

  # Apply x/y translation from <use>.
  my $x = num($elem->getAttribute('x')) // 0;
  my $y = num($elem->getAttribute('y')) // 0;
  my $m = $matrix;
  if ($x != 0 || $y != 0) {
    $m = matrix_multiply($matrix, [1, 0, 0, 1, $x, $y]);
  }

  # Process the target element (and its children if it's a <g>).
  my $tag = $target->localname();
  if ($tag eq 'g' || $tag eq 'svg') {
    # Apply target's own transform.
    my $tr = $target->getAttribute('transform');
    if (defined $tr && $tr ne '') {
      $m = matrix_multiply($m, parse_transform($tr));
    }
    process_element($target, $m, $cmds, $tol, $doc);
  } elsif ($tag eq 'path') {
    my $tr = $target->getAttribute('transform');
    if (defined $tr && $tr ne '') {
      $m = matrix_multiply($m, parse_transform($tr));
    }
    my $d = $target->getAttribute('d');
    process_path($d, $m, $cmds, $tol) if defined $d;
  } else {
    # For other shapes, wrap in process_element logic.
    my $tr = $target->getAttribute('transform');
    if (defined $tr && $tr ne '') {
      $m = matrix_multiply($m, parse_transform($tr));
    }
    # Re-dispatch single element.
    my $wrap_tag = $target->localname();
    if ($wrap_tag eq 'line') {
      process_line($target, $m, $cmds);
    } elsif ($wrap_tag eq 'polyline') {
      process_polyline($target, $m, $cmds, 0);
    } elsif ($wrap_tag eq 'polygon') {
      process_polyline($target, $m, $cmds, 1);
    } elsif ($wrap_tag eq 'rect') {
      process_rect($target, $m, $cmds, $tol);
    } elsif ($wrap_tag eq 'circle') {
      process_circle($target, $m, $cmds, $tol);
    } elsif ($wrap_tag eq 'ellipse') {
      process_ellipse($target, $m, $cmds, $tol);
    }
  }
}

# ---- Colour extraction ------------------------------------------------------

sub stroke_colour {
  my ($elem) = @_;
  # Direct stroke attribute
  my $c = $elem->getAttribute('stroke') // '';
  # Fall back to inline style: stroke:#rrggbb
  if (!$c) {
    my $style = $elem->getAttribute('style') // '';
    ($c) = $style =~ /\bstroke\s*:\s*([^;,\s]+)/i;
    $c //= '';
  }
  $c =~ s/\s+//g;
  return lc($c) if $c =~ /^#[0-9a-fA-F]{6}$/;
  return undef;
}

# ---- Shape Processors ------------------------------------------------------

sub process_line {
  my ($elem, $matrix, $cmds) = @_;
  my $x1 = num($elem->getAttribute('x1')) // 0;
  my $y1 = num($elem->getAttribute('y1')) // 0;
  my $x2 = num($elem->getAttribute('x2')) // 0;
  my $y2 = num($elem->getAttribute('y2')) // 0;
  my ($tx1, $ty1) = transform_point($matrix, $x1, $y1);
  my ($tx2, $ty2) = transform_point($matrix, $x2, $y2);
  push @$cmds, { cmd => 'moveto', x => $tx1, y => $ty1 };
  push @$cmds, { cmd => 'drawto', x => $tx2, y => $ty2 };
}

sub process_polyline {
  my ($elem, $matrix, $cmds, $close) = @_;
  my $points_str = $elem->getAttribute('points') // '';
  my @nums = ($points_str =~ /([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)/g);
  return if @nums < 4;
  my @points;
  while (@nums >= 2) {
    push @points, [shift @nums, shift @nums];
  }
  my ($fx, $fy) = transform_point($matrix, $points[0][0], $points[0][1]);
  push @$cmds, { cmd => 'moveto', x => $fx, y => $fy };
  for my $i (1 .. $#points) {
    my ($tx, $ty) = transform_point($matrix, $points[$i][0], $points[$i][1]);
    push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
  }
  if ($close && @points > 2) {
    push @$cmds, { cmd => 'drawto', x => $fx, y => $fy };
  }
}

sub process_rect {
  my ($elem, $matrix, $cmds, $tol) = @_;
  my $x  = num($elem->getAttribute('x'))      // 0;
  my $y  = num($elem->getAttribute('y'))      // 0;
  my $w  = num($elem->getAttribute('width'))  // 0;
  my $h  = num($elem->getAttribute('height')) // 0;
  my $rx = num($elem->getAttribute('rx'));
  my $ry = num($elem->getAttribute('ry'));
  return if $w <= 0 || $h <= 0;

  # Clamp rx/ry per SVG spec.
  $rx //= $ry // 0;
  $ry //= $rx;
  $rx = $w / 2 if $rx > $w / 2;
  $ry = $h / 2 if $ry > $h / 2;

  if ($rx > 0 && $ry > 0) {
    # Rounded rect as path with arcs.
    my $d = sprintf(
      "M %.6f %.6f " .
      "L %.6f %.6f A %.6f %.6f 0 0 1 %.6f %.6f " .
      "L %.6f %.6f A %.6f %.6f 0 0 1 %.6f %.6f " .
      "L %.6f %.6f A %.6f %.6f 0 0 1 %.6f %.6f " .
      "L %.6f %.6f A %.6f %.6f 0 0 1 %.6f %.6f Z",
      $x + $rx, $y,
      $x + $w - $rx, $y, $rx, $ry, $x + $w, $y + $ry,
      $x + $w, $y + $h - $ry, $rx, $ry, $x + $w - $rx, $y + $h,
      $x + $rx, $y + $h, $rx, $ry, $x, $y + $h - $ry,
      $x, $y + $ry, $rx, $ry, $x + $rx, $y
    );
    process_path($d, $matrix, $cmds, $tol);
  } else {
    # Simple rect.
    my @corners = (
      [$x,      $y],
      [$x + $w, $y],
      [$x + $w, $y + $h],
      [$x,      $y + $h],
    );
    my ($fx, $fy) = transform_point($matrix, $corners[0][0], $corners[0][1]);
    push @$cmds, { cmd => 'moveto', x => $fx, y => $fy };
    for my $i (1 .. 3) {
      my ($tx, $ty) = transform_point($matrix, $corners[$i][0], $corners[$i][1]);
      push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
    }
    push @$cmds, { cmd => 'drawto', x => $fx, y => $fy };
  }
}

sub process_circle {
  my ($elem, $matrix, $cmds, $tol) = @_;
  my $cx = num($elem->getAttribute('cx')) // 0;
  my $cy = num($elem->getAttribute('cy')) // 0;
  my $r  = num($elem->getAttribute('r'))  // 0;
  return if $r <= 0;
  # Circle as four arcs (two semicircles via path).
  my $d = sprintf(
    "M %.6f %.6f A %.6f %.6f 0 1 1 %.6f %.6f A %.6f %.6f 0 1 1 %.6f %.6f Z",
    $cx + $r, $cy,
    $r, $r, $cx - $r, $cy,
    $r, $r, $cx + $r, $cy
  );
  process_path($d, $matrix, $cmds, $tol);
}

sub process_ellipse {
  my ($elem, $matrix, $cmds, $tol) = @_;
  my $cx = num($elem->getAttribute('cx')) // 0;
  my $cy = num($elem->getAttribute('cy')) // 0;
  my $rx = num($elem->getAttribute('rx')) // 0;
  my $ry = num($elem->getAttribute('ry')) // 0;
  return if $rx <= 0 || $ry <= 0;
  my $d = sprintf(
    "M %.6f %.6f A %.6f %.6f 0 1 1 %.6f %.6f A %.6f %.6f 0 1 1 %.6f %.6f Z",
    $cx + $rx, $cy,
    $rx, $ry, $cx - $rx, $cy,
    $rx, $ry, $cx + $rx, $cy
  );
  process_path($d, $matrix, $cmds, $tol);
}

# ---- Path Parsing & Flattening ---------------------------------------------

sub process_path {
  my ($d, $matrix, $cmds, $tol) = @_;
  my @segments = parse_path_data($d);
  flatten_path(\@segments, $matrix, $cmds, $tol);
}

sub parse_path_data {
  my ($d) = @_;

  # Tokenise: commands and numbers.
  my @tokens;
  while ($d =~ /([MmLlHhVvCcSsQqTtAaZz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)/g) {
    push @tokens, $1;
  }

  my @segments;
  my $cmd = '';
  my @args;

  my %arg_count = (
    M => 2, L => 2, H => 1, V => 1,
    C => 6, S => 4, Q => 4, T => 2,
    A => 7, Z => 0,
  );

  for (my $i = 0; $i < @tokens; $i++) {
    my $tok = $tokens[$i];
    if ($tok =~ /^[MmLlHhVvCcSsQqTtAaZz]$/) {
      $cmd = $tok;
      @args = ();
      if (uc($cmd) eq 'Z') {
        push @segments, { cmd => $cmd };
      }
    } else {
      # Arc flags (args 3 and 4, 0-based) may be packed with no separator.
      if (uc($cmd) eq 'A' && (@args == 3 || @args == 4)
          && length($tok) > 1) {
        my $flag = substr($tok, 0, 1) + 0;
        push @args, $flag;
        my $rest = substr($tok, 1);
        if ($rest ne '') {
          splice(@tokens, $i + 1, 0, $rest);
        }
      } else {
        push @args, $tok + 0;
      }
      my $need = $arg_count{uc($cmd)} // 0;
      if ($need > 0 && @args == $need) {
        push @segments, { cmd => $cmd, args => [@args] };
        @args = ();
        # Implicit repeat: M becomes L, m becomes l.
        if ($cmd eq 'M') { $cmd = 'L'; }
        elsif ($cmd eq 'm') { $cmd = 'l'; }
      }
    }
  }

  return @segments;
}

sub flatten_path {
  my ($segments, $matrix, $cmds, $tol) = @_;

  my ($cx, $cy) = (0, 0);  # Current point.
  my ($sx, $sy) = (0, 0);  # Subpath start.
  my ($last_cx2, $last_cy2);  # Last control point for S/T.
  my $last_cmd = '';

  for my $seg (@$segments) {
    my $c = $seg->{cmd};
    my $a = $seg->{args};
    my $uc = uc($c);
    my $rel = ($c eq lc($c)) ? 1 : 0;

    if ($uc eq 'M') {
      my ($x, $y) = @$a;
      ($x, $y) = ($cx + $x, $cy + $y) if $rel;
      my ($tx, $ty) = transform_point($matrix, $x, $y);
      push @$cmds, { cmd => 'moveto', x => $tx, y => $ty };
      ($cx, $cy) = ($x, $y);
      ($sx, $sy) = ($x, $y);
      $last_cx2 = $last_cy2 = undef;
    } elsif ($uc eq 'L') {
      my ($x, $y) = @$a;
      ($x, $y) = ($cx + $x, $cy + $y) if $rel;
      my ($tx, $ty) = transform_point($matrix, $x, $y);
      push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
      ($cx, $cy) = ($x, $y);
      $last_cx2 = $last_cy2 = undef;
    } elsif ($uc eq 'H') {
      my $x = $a->[0];
      $x = $cx + $x if $rel;
      my ($tx, $ty) = transform_point($matrix, $x, $cy);
      push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
      $cx = $x;
      $last_cx2 = $last_cy2 = undef;
    } elsif ($uc eq 'V') {
      my $y = $a->[0];
      $y = $cy + $y if $rel;
      my ($tx, $ty) = transform_point($matrix, $cx, $y);
      push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
      $cy = $y;
      $last_cx2 = $last_cy2 = undef;
    } elsif ($uc eq 'C') {
      my ($x1, $y1, $x2, $y2, $x, $y) = @$a;
      if ($rel) {
        ($x1, $y1) = ($cx + $x1, $cy + $y1);
        ($x2, $y2) = ($cx + $x2, $cy + $y2);
        ($x, $y)   = ($cx + $x,  $cy + $y);
      }
      flatten_cubic($cx, $cy, $x1, $y1, $x2, $y2, $x, $y,
                    $matrix, $cmds, $tol, 0);
      ($last_cx2, $last_cy2) = ($x2, $y2);
      ($cx, $cy) = ($x, $y);
    } elsif ($uc eq 'S') {
      my ($x2, $y2, $x, $y) = @$a;
      if ($rel) {
        ($x2, $y2) = ($cx + $x2, $cy + $y2);
        ($x, $y)   = ($cx + $x,  $cy + $y);
      }
      # Reflect previous control point.
      my ($x1, $y1);
      if (defined $last_cx2 && ($last_cmd eq 'C' || $last_cmd eq 'S')) {
        ($x1, $y1) = (2 * $cx - $last_cx2, 2 * $cy - $last_cy2);
      } else {
        ($x1, $y1) = ($cx, $cy);
      }
      flatten_cubic($cx, $cy, $x1, $y1, $x2, $y2, $x, $y,
                    $matrix, $cmds, $tol, 0);
      ($last_cx2, $last_cy2) = ($x2, $y2);
      ($cx, $cy) = ($x, $y);
    } elsif ($uc eq 'Q') {
      my ($x1, $y1, $x, $y) = @$a;
      if ($rel) {
        ($x1, $y1) = ($cx + $x1, $cy + $y1);
        ($x, $y)   = ($cx + $x,  $cy + $y);
      }
      # Elevate quadratic to cubic.
      my ($cx1, $cy1) = ($cx + 2/3 * ($x1 - $cx), $cy + 2/3 * ($y1 - $cy));
      my ($cx2, $cy2) = ($x + 2/3 * ($x1 - $x),   $y + 2/3 * ($y1 - $y));
      flatten_cubic($cx, $cy, $cx1, $cy1, $cx2, $cy2, $x, $y,
                    $matrix, $cmds, $tol, 0);
      ($last_cx2, $last_cy2) = ($x1, $y1);
      ($cx, $cy) = ($x, $y);
    } elsif ($uc eq 'T') {
      my ($x, $y) = @$a;
      ($x, $y) = ($cx + $x, $cy + $y) if $rel;
      # Reflect previous quadratic control point.
      my ($x1, $y1);
      if (defined $last_cx2 && ($last_cmd eq 'Q' || $last_cmd eq 'T')) {
        ($x1, $y1) = (2 * $cx - $last_cx2, 2 * $cy - $last_cy2);
      } else {
        ($x1, $y1) = ($cx, $cy);
      }
      my ($cx1, $cy1) = ($cx + 2/3 * ($x1 - $cx), $cy + 2/3 * ($y1 - $cy));
      my ($cx2, $cy2) = ($x + 2/3 * ($x1 - $x),   $y + 2/3 * ($y1 - $y));
      flatten_cubic($cx, $cy, $cx1, $cy1, $cx2, $cy2, $x, $y,
                    $matrix, $cmds, $tol, 0);
      ($last_cx2, $last_cy2) = ($x1, $y1);
      ($cx, $cy) = ($x, $y);
    } elsif ($uc eq 'A') {
      my ($rx, $ry, $phi, $fa, $fs, $x, $y) = @$a;
      ($x, $y) = ($cx + $x, $cy + $y) if $rel;
      flatten_arc($cx, $cy, $rx, $ry, $phi, $fa, $fs, $x, $y,
                  $matrix, $cmds, $tol);
      ($cx, $cy) = ($x, $y);
      $last_cx2 = $last_cy2 = undef;
    } elsif ($uc eq 'Z') {
      if ($cx != $sx || $cy != $sy) {
        my ($tx, $ty) = transform_point($matrix, $sx, $sy);
        push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
      }
      ($cx, $cy) = ($sx, $sy);
      $last_cx2 = $last_cy2 = undef;
    }

    $last_cmd = $uc;
  }
}

# ---- Cubic Bezier Flattening (de Casteljau) ---------------------------------

sub flatten_cubic {
  my ($x0, $y0, $x1, $y1, $x2, $y2, $x3, $y3,
      $matrix, $cmds, $tol, $depth) = @_;

  if ($depth > 12) {
    my ($tx, $ty) = transform_point($matrix, $x3, $y3);
    push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
    return;
  }

  # Flatness test: max distance of control points from chord.
  my $dx = $x3 - $x0;
  my $dy = $y3 - $y0;
  my $d2 = $dx * $dx + $dy * $dy;

  my $flat = 1;
  if ($d2 > 0) {
    # Distance of control point from line (x0,y0)-(x3,y3).
    my $d1 = abs(($x1 - $x0) * ($y3 - $y0) - ($y1 - $y0) * ($x3 - $x0));
    my $d2v = abs(($x2 - $x0) * ($y3 - $y0) - ($y2 - $y0) * ($x3 - $x0));
    my $chord = sqrt($d2);
    $flat = 0 if ($d1 / $chord > $tol || $d2v / $chord > $tol);
  } else {
    # Degenerate chord — check control point distances from start.
    my $d1 = sqrt(($x1 - $x0)**2 + ($y1 - $y0)**2);
    my $d2v = sqrt(($x2 - $x0)**2 + ($y2 - $y0)**2);
    $flat = 0 if ($d1 > $tol || $d2v > $tol);
  }

  if ($flat) {
    my ($tx, $ty) = transform_point($matrix, $x3, $y3);
    push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
    return;
  }

  # de Casteljau subdivision at t=0.5.
  my $mx01  = ($x0 + $x1) / 2;   my $my01  = ($y0 + $y1) / 2;
  my $mx12  = ($x1 + $x2) / 2;   my $my12  = ($y1 + $y2) / 2;
  my $mx23  = ($x2 + $x3) / 2;   my $my23  = ($y2 + $y3) / 2;
  my $mx012 = ($mx01 + $mx12) / 2; my $my012 = ($my01 + $my12) / 2;
  my $mx123 = ($mx12 + $mx23) / 2; my $my123 = ($my12 + $my23) / 2;
  my $mx    = ($mx012 + $mx123) / 2; my $my = ($my012 + $my123) / 2;

  flatten_cubic($x0, $y0, $mx01, $my01, $mx012, $my012, $mx, $my,
                $matrix, $cmds, $tol, $depth + 1);
  flatten_cubic($mx, $my, $mx123, $my123, $mx23, $my23, $x3, $y3,
                $matrix, $cmds, $tol, $depth + 1);
}

# ---- Arc Handling -----------------------------------------------------------

sub flatten_arc {
  my ($x1, $y1, $rx, $ry, $phi_deg, $fa, $fs, $x2, $y2,
      $matrix, $cmds, $tol) = @_;

  # Degenerate cases.
  if ($rx == 0 || $ry == 0) {
    my ($tx, $ty) = transform_point($matrix, $x2, $y2);
    push @$cmds, { cmd => 'drawto', x => $tx, y => $ty };
    return;
  }
  if ($x1 == $x2 && $y1 == $y2) {
    return;
  }

  $rx = abs($rx);
  $ry = abs($ry);
  my $phi = $phi_deg * PI / 180;

  # SVG spec F.6.5: endpoint to centre parameterisation.
  my $cos_phi = cos($phi);
  my $sin_phi = sin($phi);

  my $dx2 = ($x1 - $x2) / 2;
  my $dy2 = ($y1 - $y2) / 2;
  my $x1p =  $cos_phi * $dx2 + $sin_phi * $dy2;
  my $y1p = -$sin_phi * $dx2 + $cos_phi * $dy2;

  # Ensure radii are large enough (F.6.6).
  my $lambda = ($x1p * $x1p) / ($rx * $rx) + ($y1p * $y1p) / ($ry * $ry);
  if ($lambda > 1) {
    my $s = sqrt($lambda);
    $rx *= $s;
    $ry *= $s;
  }

  my $rxsq = $rx * $rx;
  my $rysq = $ry * $ry;
  my $x1psq = $x1p * $x1p;
  my $y1psq = $y1p * $y1p;

  my $num = $rxsq * $rysq - $rxsq * $y1psq - $rysq * $x1psq;
  my $den = $rxsq * $y1psq + $rysq * $x1psq;
  my $sq = ($den > 0) ? sqrt(abs($num / $den)) : 0;
  $sq = -$sq if $fa == $fs;

  my $cxp =  $sq * $rx * $y1p / $ry;
  my $cyp = -$sq * $ry * $x1p / $rx;

  my $cx = $cos_phi * $cxp - $sin_phi * $cyp + ($x1 + $x2) / 2;
  my $cy = $sin_phi * $cxp + $cos_phi * $cyp + ($y1 + $y2) / 2;

  # Compute start angle and sweep.
  my $theta1 = angle_between(1, 0,
    ($x1p - $cxp) / $rx, ($y1p - $cyp) / $ry);
  my $dtheta = angle_between(
    ($x1p - $cxp) / $rx, ($y1p - $cyp) / $ry,
    (-$x1p - $cxp) / $rx, (-$y1p - $cyp) / $ry);

  if (!$fs && $dtheta > 0) {
    $dtheta -= 2 * PI;
  } elsif ($fs && $dtheta < 0) {
    $dtheta += 2 * PI;
  }

  # Convert arc to cubic beziers, then flatten those.
  arc_to_cubics($cx, $cy, $rx, $ry, $phi, $theta1, $dtheta,
                $matrix, $cmds, $tol);
}

sub arc_to_cubics {
  my ($cx, $cy, $rx, $ry, $phi, $theta1, $dtheta,
      $matrix, $cmds, $tol) = @_;

  # Split arc into segments of at most PI/2 (90 degrees).
  my $n = ceil(abs($dtheta) / (PI / 2));
  $n = 1 if $n < 1;
  my $step = $dtheta / $n;
  my $cos_phi = cos($phi);
  my $sin_phi = sin($phi);

  for my $i (0 .. $n - 1) {
    my $t1 = $theta1 + $step * $i;
    my $t2 = $t1 + $step;
    arc_segment_to_cubic($cx, $cy, $rx, $ry, $cos_phi, $sin_phi,
                         $t1, $t2, $matrix, $cmds, $tol);
  }
}

sub arc_segment_to_cubic {
  my ($cx, $cy, $rx, $ry, $cos_phi, $sin_phi,
      $t1, $t2, $matrix, $cmds, $tol) = @_;

  # Approximate a small arc segment with a cubic bezier.
  my $alpha = sin($t2 - $t1) * (sqrt(4 + 3 * tan(($t2 - $t1) / 2)**2) - 1) / 3;

  my $cos_t1 = cos($t1);  my $sin_t1 = sin($t1);
  my $cos_t2 = cos($t2);  my $sin_t2 = sin($t2);

  # Endpoint in ellipse space.
  my $ex1 = $rx * $cos_t1;  my $ey1 = $ry * $sin_t1;
  my $ex2 = $rx * $cos_t2;  my $ey2 = $ry * $sin_t2;

  # Derivatives.
  my $dx1 = -$rx * $sin_t1;  my $dy1 = $ry * $cos_t1;
  my $dx2 = -$rx * $sin_t2;  my $dy2 = $ry * $cos_t2;

  # Control points in ellipse space.
  my $cp1x = $ex1 + $alpha * $dx1;
  my $cp1y = $ey1 + $alpha * $dy1;
  my $cp2x = $ex2 - $alpha * $dx2;
  my $cp2y = $ey2 - $alpha * $dy2;

  # Rotate and translate to world space.
  my $x0 = $cos_phi * $ex1  - $sin_phi * $ey1  + $cx;
  my $y0 = $sin_phi * $ex1  + $cos_phi * $ey1  + $cy;
  my $x1 = $cos_phi * $cp1x - $sin_phi * $cp1y + $cx;
  my $y1 = $sin_phi * $cp1x + $cos_phi * $cp1y + $cy;
  my $x2 = $cos_phi * $cp2x - $sin_phi * $cp2y + $cx;
  my $y2 = $sin_phi * $cp2x + $cos_phi * $cp2y + $cy;
  my $x3 = $cos_phi * $ex2  - $sin_phi * $ey2  + $cx;
  my $y3 = $sin_phi * $ex2  + $cos_phi * $ey2  + $cy;

  flatten_cubic($x0, $y0, $x1, $y1, $x2, $y2, $x3, $y3,
                $matrix, $cmds, $tol, 0);
}

sub angle_between {
  my ($ux, $uy, $vx, $vy) = @_;
  my $dot = $ux * $vx + $uy * $vy;
  my $len = sqrt($ux**2 + $uy**2) * sqrt($vx**2 + $vy**2);
  return 0 if $len == 0;
  my $cos_a = $dot / $len;
  $cos_a =  1 if $cos_a >  1;
  $cos_a = -1 if $cos_a < -1;
  my $angle = acos($cos_a);
  $angle = -$angle if ($ux * $vy - $uy * $vx) < 0;
  return $angle;
}

sub acos {
  return POSIX::acos($_[0]);
}

# ---- Transform / Matrix Math -----------------------------------------------

# Matrix is [a, b, c, d, e, f] representing:
#   | a c e |
#   | b d f |
#   | 0 0 1 |

sub identity_matrix { return [1, 0, 0, 1, 0, 0]; }

sub matrix_multiply {
  my ($m1, $m2) = @_;
  my ($a1, $b1, $c1, $d1, $e1, $f1) = @$m1;
  my ($a2, $b2, $c2, $d2, $e2, $f2) = @$m2;
  return [
    $a1*$a2 + $c1*$b2,
    $b1*$a2 + $d1*$b2,
    $a1*$c2 + $c1*$d2,
    $b1*$c2 + $d1*$d2,
    $a1*$e2 + $c1*$f2 + $e1,
    $b1*$e2 + $d1*$f2 + $f1,
  ];
}

sub transform_point {
  my ($m, $x, $y) = @_;
  return (
    $m->[0] * $x + $m->[2] * $y + $m->[4],
    $m->[1] * $x + $m->[3] * $y + $m->[5],
  );
}

sub parse_transform {
  my ($str) = @_;
  my $m = identity_matrix();

  while ($str =~ /(\w+)\s*\(([^)]*)\)/g) {
    my $func = $1;
    my @args = ($2 =~ /([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)/g);
    @args = map { $_ + 0 } @args;

    my $t;
    if ($func eq 'translate') {
      my $tx = $args[0] // 0;
      my $ty = $args[1] // 0;
      $t = [1, 0, 0, 1, $tx, $ty];
    } elsif ($func eq 'scale') {
      my $sx = $args[0] // 1;
      my $sy = $args[1] // $sx;
      $t = [$sx, 0, 0, $sy, 0, 0];
    } elsif ($func eq 'rotate') {
      my $a = ($args[0] // 0) * PI / 180;
      my $cos_a = cos($a);
      my $sin_a = sin($a);
      if (@args >= 3) {
        # rotate(angle, cx, cy)
        my ($rcx, $rcy) = ($args[1], $args[2]);
        $t = matrix_multiply(
          [1, 0, 0, 1, $rcx, $rcy],
          matrix_multiply(
            [$cos_a, $sin_a, -$sin_a, $cos_a, 0, 0],
            [1, 0, 0, 1, -$rcx, -$rcy]
          )
        );
      } else {
        $t = [$cos_a, $sin_a, -$sin_a, $cos_a, 0, 0];
      }
    } elsif ($func eq 'matrix') {
      $t = [@args[0..5]];
    } elsif ($func eq 'skewX') {
      my $a = ($args[0] // 0) * PI / 180;
      $t = [1, 0, tan($a), 1, 0, 0];
    } elsif ($func eq 'skewY') {
      my $a = ($args[0] // 0) * PI / 180;
      $t = [1, tan($a), 0, 1, 0, 0];
    }

    $m = matrix_multiply($m, $t) if defined $t;
  }

  return $m;
}

sub tan {
  return sin($_[0]) / cos($_[0]);
}

# ---- viewBox / Document Size ------------------------------------------------

sub compute_viewbox_transform {
  my ($svg) = @_;

  my $w_attr = $svg->getAttribute('width');
  my $h_attr = $svg->getAttribute('height');
  my $vb     = $svg->getAttribute('viewBox');

  # Parse document width/height to mm.
  my $doc_w = defined $w_attr ? parse_length($w_attr) : undef;
  my $doc_h = defined $h_attr ? parse_length($h_attr) : undef;

  # Parse viewBox.
  my ($vb_x, $vb_y, $vb_w, $vb_h);
  my $num_re = '[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?';
  if (defined $vb && $vb =~ /^\s*($num_re)[\s,]+($num_re)[\s,]+($num_re)[\s,]+($num_re)\s*$/o) {
    ($vb_x, $vb_y, $vb_w, $vb_h) = ($1+0, $2+0, $3+0, $4+0);
  }

  my $matrix = identity_matrix();
  my ($svg_w, $svg_h);

  my $par = $svg->getAttribute('preserveAspectRatio') // '';

  if (defined $vb_w && defined $doc_w) {
    # Both viewBox and width/height: scale viewBox to fit document size.
    $svg_w = $doc_w;
    $svg_h = $doc_h;
    my $sx = $doc_w / $vb_w;
    my $sy = $doc_h / $vb_h;
    if ($par =~ /none/i) {
      # Stretch to fill — independent X/Y scaling.
      my $tx = -$vb_x * $sx;
      my $ty = -$vb_y * $sy;
      $matrix = [$sx, 0, 0, $sy, $tx, $ty];
    } else {
      # Default xMidYMid meet — uniform scale, centred.
      my $s = ($sx < $sy) ? $sx : $sy;
      my $tx = ($doc_w - $vb_w * $s) / 2 - $vb_x * $s;
      my $ty = ($doc_h - $vb_h * $s) / 2 - $vb_y * $s;
      $matrix = [$s, 0, 0, $s, $tx, $ty];
    }
  } elsif (defined $vb_w) {
    # viewBox only, no width/height: use viewBox dimensions as mm.
    $svg_w = $vb_w;
    $svg_h = $vb_h;
    if ($vb_x != 0 || $vb_y != 0) {
      $matrix = [1, 0, 0, 1, -$vb_x, -$vb_y];
    }
  } elsif (defined $doc_w) {
    # width/height only, no viewBox.
    $svg_w = $doc_w;
    $svg_h = $doc_h;
  } else {
    # Neither — assume 393x262 (full plotter area).
    $svg_w = 393;
    $svg_h = 262;
  }

  return ($svg_w, $svg_h, $matrix);
}

sub parse_length {
  my ($str) = @_;
  return undef unless defined $str;
  if ($str =~ /^([-\d.]+)\s*(mm|cm|in|pt|px|pc|em|ex|%)?$/i) {
    my ($val, $unit) = ($1 + 0, lc($2 // 'px'));
    if ($unit eq 'mm')    { return $val; }
    elsif ($unit eq 'cm') { return $val * 10; }
    elsif ($unit eq 'in') { return $val * 25.4; }
    elsif ($unit eq 'pt') { return $val * 25.4 / 72; }
    elsif ($unit eq 'pc') { return $val * 25.4 / 6; }
    elsif ($unit eq 'px') { return $val * 25.4 / 96; }
    else                  { return $val * 25.4 / 96; }  # em/ex/% fallback
  }
  # Bare number.
  if ($str =~ /^([-\d.]+)$/) {
    return $1 * 25.4 / 96;  # Default px.
  }
  return undef;
}

# ---- Utilities --------------------------------------------------------------

sub num {
  my ($str) = @_;
  return undef unless defined $str && $str =~ /^[-+]?\d*\.?\d+/;
  return $str + 0;
}

sub bounding_box {
  my ($cmds) = @_;
  my ($min_x, $min_y, $max_x, $max_y);
  for my $c (@$cmds) {
    if (!defined $min_x) {
      ($min_x, $min_y, $max_x, $max_y) = ($c->{x}, $c->{y}, $c->{x}, $c->{y});
    } else {
      $min_x = $c->{x} if $c->{x} < $min_x;
      $min_y = $c->{y} if $c->{y} < $min_y;
      $max_x = $c->{x} if $c->{x} > $max_x;
      $max_y = $c->{y} if $c->{y} > $max_y;
    }
  }
  return ($min_x, $min_y, $max_x, $max_y);
}

1;
