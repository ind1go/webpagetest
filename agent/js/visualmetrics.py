#!/usr/bin/python
"""
Copyright (c) 2014, Google Inc.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the name of the company nor the names of its contributors may be
      used to endorse or promote products derived from this software without
      specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE."""
import glob
import gzip
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile


########################################################################################################################
#   Frame Extraction and de-duplication
########################################################################################################################


def video_to_frames(video, directory, force, orange_file, find_viewport, full_resolution, timeline_file):
    first_frame = os.path.join(directory, 'ms_000000')
    if (not os.path.isfile(first_frame + '.png') and not os.path.isfile(first_frame + '.jpg')) or force:
        if os.path.isfile(video):
            video = os.path.realpath(video)
            logging.info("Processing frames from video " + video + " to " + directory)
            if not os.path.isdir(directory):
                os.mkdir(directory, 0644)
            if os.path.isdir(directory):
                directory = os.path.realpath(directory)
                clean_directory(directory)
                viewport = find_video_viewport(video, directory, find_viewport)
                if extract_frames(video, directory, full_resolution, viewport):
                    if orange_file is not None:
                        remove_orange_frames(directory, orange_file)
                    adjust_frame_times(directory)
                    if timeline_file is not None:
                        synchronize_to_timeline(directory, timeline_file)
                    eliminate_duplicate_frames(directory)
                else:
                    logging.critical("Error extracting the video frames from " + video)
            else:
                logging.critical("Error creating output directory: " + directory)
        else:
            logging.critical("Input video file " + video + " does not exist")
    else:
        logging.info("Extracted video already exists in " + directory)


def extract_frames(video, directory, full_resolution, viewport):
    ok = False
    logging.info("Extracting frames from " + video + " to " + directory)
    decimate = get_decimate_filter()
    if decimate is not None:
        crop = ''
        if viewport is not None:
            crop = 'crop={0}:{1}:{2}:{3},'.format(viewport['width'], viewport['height'], viewport['x'], viewport['y'])
        scale = 'scale=iw*min(400/iw\,400/ih):ih*min(400/iw\,400/ih),'
        if full_resolution:
            scale = ''
        command = ['ffmpeg', '-v', 'debug', '-i', video, '-vsync',  '0',
                   '-vf', crop + scale + decimate + '=0:64:640:0.001',
                   os.path.join(directory, 'img-%d.png')]
        logging.debug(' '.join(command))
        lines = []
        p = subprocess.Popen(command, stderr=subprocess.PIPE)
        while p.poll() is None:
            lines.extend(iter(p.stderr.readline, ""))

        match = re.compile('keep pts:[0-9]+ pts_time:(?P<timecode>[0-9\.]+)')
        frame_count = 0
        for line in lines:
            m = re.search(match, line)
            if m:
                frame_count += 1
                frame_time = int(math.ceil(float(m.groupdict().get('timecode')) * 1000))
                src = os.path.join(directory, 'img-{0:d}.png'.format(frame_count))
                dest = os.path.join(directory, 'video-{0:06d}.png'.format(frame_time))
                logging.debug('Renaming ' + src + ' to ' + dest)
                os.rename(src, dest)
                ok = True

    return ok


def remove_orange_frames(directory, orange_file):
    frames = sorted(glob.glob(os.path.join(directory, 'video-*.png')))
    for frame in frames:
        if is_orange_frame(frame, orange_file):
            logging.debug("Removing orange frame " + frame)
            os.remove(frame)
        else:
            break
    for frame in reversed(frames):
        if is_orange_frame(frame, orange_file):
            logging.debug("Removing orange frame " + frame + " from the end")
            os.remove(frame)
        else:
            break


def find_video_viewport(video, directory, find_viewport):
    viewport = None
    try:
        from PIL import Image
        frame = os.path.join(directory, 'viewport.png')
        if os.path.isfile(frame):
            os.remove(frame)
        subprocess.check_output(['ffmpeg', '-i', video, '-frames:v', '1', frame])
        if os.path.isfile(frame):
            im = Image.open(frame)
            width, height = im.size
            logging.debug('{0} is {1:d}x{2:d}'.format(frame, width, height))
            if find_viewport:
                x = int(math.floor(width / 2))
                y = int(math.floor(height / 2))
                pixels = im.load()
                background = pixels[x, y]

                # Find the left edge
                left = None
                while left is None and x >= 0:
                    if not colors_are_similar(background, pixels[x, y]):
                        left = x + 1
                    else:
                        x -= 1
                if left is None:
                    left = 0
                logging.debug('Viewport left edge is {0:d}'.format(left))

                # Find the right edge
                x = int(math.floor(width / 2))
                right = None
                while right is None and x < width:
                    if not colors_are_similar(background, pixels[x, y]):
                        right = x - 1
                    else:
                        x += 1
                if right is None:
                    right = width
                logging.debug('Viewport right edge is {0:d}'.format(right))

                # Find the top edge
                x = int(math.floor(width / 2))
                top = None
                while top is None and y >= 0:
                    if not colors_are_similar(background, pixels[x, y]):
                        top = y + 1
                    else:
                        y -= 1
                if top is None:
                    top = 0
                logging.debug('Viewport top edge is {0:d}'.format(top))

                # Find the bottom edge
                y = int(math.floor(height / 2))
                bottom = None
                while bottom is None and y < height:
                    if not colors_are_similar(background, pixels[x, y]):
                        bottom = y - 1
                    else:
                        y +=1
                if bottom is None:
                    bottom = height
                logging.debug('Viewport bottom edge is {0:d}'.format(bottom))

                viewport = {'x': left, 'y': top, 'width': (right - left), 'height': (bottom - top)}
            else:
                viewport = {'x': 0, 'y': 0, 'width': width, 'height': height}
            os.remove(frame)

    except Exception as e:
        viewport = None

    return viewport


def adjust_frame_times(directory):
    offset = None
    frames = sorted(glob.glob(os.path.join(directory, 'video-*.png')))
    match = re.compile('video-(?P<ms>[0-9]+)\.png')
    for frame in frames:
        m = re.search(match, frame)
        if m is not None:
            frame_time = int(m.groupdict().get('ms'))
            if offset is None:
                offset = frame_time
            new_time = frame_time - offset
            dest = os.path.join(directory, 'ms_{0:06d}.png'.format(new_time))
            os.rename(frame, dest)


def eliminate_duplicate_frames(directory):
    try:
        # Do a first pass looking for the first non-blank frame with an allowance
        # for up to a 2% per-pixel difference for noise in the white field.
        files = sorted(glob.glob(os.path.join(directory, 'ms_*.png')))
        blank = files[0]

        from PIL import Image
        im = Image.open(blank)
        width, height = im.size
        top = 6
        right_margin = 6
        bottom_margin = 6
        if height > 400 or width > 400:
            top = int(math.ceil(float(height) * 0.03))
            right_margin = int(math.ceil(float(width) * 0.03))
            bottom_margin = int(math.ceil(float(width) * 0.03))
        height = max(height - top - bottom_margin, 1)
        left = 0
        width = max(width - right_margin, 1)
        crop = '{0:d}x{1:d}+{2:d}+{3:d}'.format(width, height, left, top)
        logging.debug('Viewport cropping set to ' + crop)

        count = len(files)
        for i in range (1, count):
            if frames_match(blank, files[i], 2, crop):
                logging.debug('Removing duplicate frame {0} from the beginning'.format(files[i]))
                os.remove(files[i])
            else:
                break

        # Do a second pass looking for the last frame but with an allowance for up
        # to a 10% difference in individual pixels to deal with noise around text.
        files = sorted(glob.glob(os.path.join(directory, 'ms_*.png')))
        count = len(files)
        duplicates = []
        if count > 2:
            files.reverse()
            baseline = files[0]
            previous_frame = baseline
            for i in range (1, count):
                if frames_match(baseline, files[i], 10, crop):
                    if previous_frame is baseline:
                        duplicates.append(previous_frame)
                    else:
                        logging.debug('Removing duplicate frame {0} from the end'.format(previous_frame))
                        os.remove(previous_frame)
                    previous_frame = files[i]
                else:
                    break
        for duplicate in duplicates:
            logging.debug('Removing duplicate frame {0} from the end'.format(duplicate))
            os.remove(duplicate)

    except:
        logging.exception('Error processing frames for duplicates')


def get_decimate_filter():
    decimate = None
    try:
        filters = subprocess.check_output(['ffmpeg', '-filters'], stderr=subprocess.STDOUT)
        lines = filters.split("\n")
        match = re.compile('(?P<filter>[\w]*decimate).*V->V.*Remove near-duplicate frames')
        for line in lines:
            m = re.search(match, line)
            if m is not None:
                matches = m.groupdict()
                decimate = m.groupdict().get('filter')
                break
    except:
        logging.critical('Error checking ffmpeg filters for decimate')
        decimate = None
    return decimate


def clean_directory(directory):
    files = glob.glob(os.path.join(directory, '*.png'))
    for file in files:
        os.remove(file)
    files = glob.glob(os.path.join(directory, '*.jpg'))
    for file in files:
        os.remove(file)
    files = glob.glob(os.path.join(directory, '*.json'))
    for file in files:
        os.remove(file)


def is_orange_frame(file, orange_file):
    orange = False
    if os.path.isfile(orange_file):
        command = ('convert "{0}" "(" "{1}" -gravity Center -crop 50x33%+0+0 -resize 200x200! ")" miff:- | '
                   'compare -metric AE - -fuzz 10% null:').format(orange_file, file)
        compare = subprocess.Popen(command, stderr=subprocess.PIPE, shell=True)
        out, err = compare.communicate()
        if re.match('^[0-9]+$', err):
            different_pixels = int(err)
            if different_pixels < 100:
                orange = True

    return orange


def colors_are_similar(a, b):
    similar = True
    for x in range (0, 3):
        if abs(a[x] - b[x]) > 25:
            similar = False

    return similar

def frames_match(image1, image2, fuzz_percent, crop_region):
    match = False
    fuzz = ''
    if fuzz_percent > 0:
        fuzz = '-fuzz {0:d}% '.format(fuzz_percent)
    crop = ''
    if crop_region is not None:
        crop = '-crop {0} '.format(crop_region)
    command = 'convert "{0}" "{1}" {2}miff:- | compare -metric AE - {3}null:'.format(image1, image2, crop, fuzz)
    compare = subprocess.Popen(command, stderr=subprocess.PIPE, shell=True)
    out, err = compare.communicate()
    if re.match('^[0-9]+$', err):
        different_pixels = int(err)
        if different_pixels == 0:
            match = True

    return match


def generate_orange_png(orange_file):
    try:
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (200,200))
        draw = ImageDraw.Draw(im)
        draw.rectangle([0,0,200,200], fill=(222,100,13))
        del draw
        im.save(orange_file, 'PNG')
    except:
        logging.exception('Error generating orange png ' + orange_file)


def synchronize_to_timeline(directory, timeline_file):
    offset = get_timeline_offset(timeline_file)
    if offset > 0:
        frames = sorted(glob.glob(os.path.join(directory, 'ms_*.png')))
        match = re.compile('ms_(?P<ms>[0-9]+)\.png')
        for frame in frames:
            m = re.search(match, frame)
            if m is not None:
                frame_time = int(m.groupdict().get('ms'))
                new_time = max(frame_time - offset, 0)
                dest = os.path.join(directory, 'ms_{0:06d}.png'.format(new_time))
                if frame != dest:
                    if os.path.isfile(dest):
                        os.remove(dest)
                    os.rename(frame, dest)


def get_timeline_offset(timeline_file):
    offset = 0
    try:
        file_name, ext = os.path.splitext(timeline_file)
        if ext.lower() == '.gz':
            f = gzip.open(timeline_file, 'rb')
        else:
            f = open(timeline_file, 'r')
        timeline = json.load(f)
        f.close()
        last_paint = None
        first_navigate = None

        # In the case of a trace instead of a timeline we want the list of events
        if 'traceEvents' in timeline:
            timeline = timeline['traceEvents']

        for timeline_event in timeline:
            paint_time = get_timeline_event_paint_time(timeline_event)
            if paint_time is not None:
                last_paint = paint_time
            first_navigate = get_timeline_event_navigate_time(timeline_event)
            if first_navigate is not None:
                break

        if last_paint is not None and first_navigate is not None and first_navigate > last_paint:
            offset = int(round(first_navigate - last_paint))
            logging.info(
                "Trimming {0:d}ms from the start of the video based on timeline synchronization".format(offset))
    except:
        logging.critical("Error processing timeline file " + timeline_file)

    return offset


def get_timeline_event_paint_time(timeline_event):
    paint_time = None
    if 'cat' in timeline_event:
        if (timeline_event['cat'].find('devtools.timeline') >= 0 and
                    'ts' in timeline_event and
                    'name' in timeline_event and (timeline_event['name'].find('Paint') >= 0 or
                                                  timeline_event['name'].find('CompositeLayers') >= 0)):
            paint_time = float(timeline_event['ts']) / 1000.0
            if 'dur' in timeline_event:
                paint_time += float(timeline_event['dur']) / 1000.0
    elif 'method' in timeline_event:
        if (timeline_event['method'] == 'Timeline.eventRecorded' and
                    'params' in timeline_event and 'record' in timeline_event['params']):
            paint_time = get_timeline_event_paint_time(timeline_event['params']['record'])
    else:
        if ('type' in timeline_event and
              (timeline_event['type'] == 'Rasterize' or
                       timeline_event['type'] == 'CompositeLayers' or
                       timeline_event['type'] == 'Paint')):
            if 'endTime' in timeline_event:
                paint_time = timeline_event['endTime']
            elif 'startTime' in timeline_event:
                paint_time = timeline_event['startTime']

        # Check for any child paint events
        if 'children' in timeline_event:
            for child in timeline_event['children']:
                child_paint_time = get_timeline_event_paint_time(child)
                if child_paint_time is not None and (paint_time is None or child_paint_time > paint_time):
                    paint_time = child_paint_time

    return paint_time


def get_timeline_event_navigate_time(timeline_event):
    navigate_time = None
    if 'cat' in timeline_event:
        if (timeline_event['cat'].find('devtools.timeline') >= 0 and
                    'ts' in timeline_event and
                    'name' in timeline_event and timeline_event['name'] == 'ResourceSendRequest'):
            navigate_time = float(timeline_event['ts']) / 1000.0
    elif 'method' in timeline_event:
        if (timeline_event['method'] == 'Timeline.eventRecorded' and
                    'params' in timeline_event and 'record' in timeline_event['params']):
            navigate_time = get_timeline_event_navigate_time(timeline_event['params']['record'])
    else:
        if ('type' in timeline_event and
                    timeline_event['type'] == 'ResourceSendRequest' and
                    'startTime' in timeline_event):
            navigate_time = timeline_event['startTime']

        # Check for any child paint events
        if 'children' in timeline_event:
            for child in timeline_event['children']:
                child_navigate_time = get_timeline_event_navigate_time(child)
                if child_navigate_time is not None and (navigate_time is None or child_navigate_time < navigate_time):
                    navigate_time = child_navigate_time

    return navigate_time


########################################################################################################################
#   Histogram calculations
########################################################################################################################


def calculate_histograms(directory, histograms_file, force):
    if not os.path.isfile(histograms_file) or force:
        try:
            extension = None
            directory = os.path.realpath(directory)
            first_frame = os.path.join(directory, 'ms_000000')
            if os.path.isfile(first_frame + '.png'):
                extension = '.png'
            elif os.path.isfile(first_frame + '.jpg'):
                extension = '.jpg'
            if extension is not None:
                histograms = []
                frames = sorted(glob.glob(os.path.join(directory, 'ms_*' + extension)))
                match = re.compile('ms_(?P<ms>[0-9]+)\.')
                for frame in frames:
                    m = re.search(match, frame)
                    if m is not None:
                        frame_time = int(m.groupdict().get('ms'))
                        histogram = calculate_image_histogram(frame)
                        if histogram is not None:
                            histograms.append({'time': frame_time, 'histogram': histogram})
                if os.path.isfile(histograms_file):
                    os.remove(histograms_file)
                f = gzip.open(histograms_file, 'wb')
                json.dump(histograms, f)
                f.close()
            else:
                logging.critical('No video frames found in ' + directory)
        except:
            logging.exception('Error calculating histograms')
    else:
        logging.debug('Histograms file {0} already exists'.format(histograms_file))


def calculate_image_histogram(file):
    logging.debug('Calculating histogram for ' + file)
    try:
        from PIL import Image
        im = Image.open(file)
        width, height = im.size
        pixels = im.load()
        histogram = {'r': [0 for i in range(256)],
                     'g': [0 for i in range(256)],
                     'b': [0 for i in range(256)]}
        for y in range (0, height):
            for x in range (0, width):
                pixel = pixels[x,y]
                # Don't include White pixels (with a tiny bit of slop for compression artifacts)
                if pixel[0] < 250 or pixel[1] < 250 or pixel[2] < 250:
                    histogram['r'][pixel[0]] += 1
                    histogram['g'][pixel[1]] += 1
                    histogram['b'][pixel[2]] += 1
    except:
        histogram = None
        logging.exception('Error calculating histogram for ' + file)
    return histogram


########################################################################################################################
#   JPEG conversion
########################################################################################################################


def convert_to_jpeg(directory, quality):
    directory = os.path.realpath(directory)
    files = sorted(glob.glob(os.path.join(directory, 'ms_*.png')))
    match = re.compile('(?P<base>ms_[0-9]+\.)')
    for file in files:
        m = re.search(match, file)
        if m is not None:
            dest = os.path.join(directory, m.groupdict().get('base') + 'jpg')
            if os.path.isfile(dest):
                os.remove(dest)
            command = 'convert "{0}" -quality {1:d} "{2}"'.format(file, quality, dest)
            subprocess.call(command, shell=True)
            if os.path.isfile(dest):
                os.remove(file)


########################################################################################################################
#   Visual Metrics
########################################################################################################################


def calculate_visual_metrics(histograms_file, start, end):
    metrics = None
    histograms = load_histograms(histograms_file, start, end)
    if histograms is not None and len(histograms) > 0:
        progress = calculate_visual_progress(histograms)
        if len(histograms) > 1:
            metrics = [
                {'name': 'First Visual Change', 'value': histograms[1]['time']},
                {'name': 'Last Visual Change', 'value': histograms[-1]['time']},
                {'name': 'Visually Complete', 'value': find_visually_complete(progress)},
                {'name': 'Speed Index', 'value': calculate_speed_index(progress)}
            ]
        else:
            metrics = [
                {'name': 'First Visual Change', 'value': histograms[0]['time']},
                {'name': 'Last Visual Change', 'value': histograms[0]['time']},
                {'name': 'Visually Complete', 'value': histograms[0]['time']},
                {'name': 'Speed Index', 'value': 0}
            ]
        prog = ''
        for p in progress:
            if len(prog):
                prog += ", "
            prog += '{0:d}={1:d}%'.format(p['time'], int(p['progress']))
        metrics.append({'name': 'Visual Progress', 'value': prog})

    return metrics


def load_histograms(histograms_file, start, end):
    histograms = None
    if os.path.isfile(histograms_file):
        f = gzip.open(histograms_file)
        original = json.load(f)
        f.close()
        if start != 0 or end != 0:
            histograms = []
            for histogram in original:
                if histogram['time'] <= start:
                    histogram['time'] = start
                    histograms = [histogram]
                elif histogram['time'] <= end:
                    histograms.append(histogram)
                else:
                    break
        else:
            histograms = original
    return histograms


def calculate_visual_progress(histograms):
    progress = []
    first = histograms[0]['histogram']
    last = histograms[-1]['histogram']
    for index, histogram in enumerate(histograms):
        p = calculate_frame_progress(histogram['histogram'], first, last)
        progress.append({'time': histogram['time'],
                         'progress': p})
        logging.debug('{0:d}ms - {1:d}% Complete'.format(histogram['time'], int(p)))
    return progress


def calculate_frame_progress(histogram, start, final):
    total = 0;
    matched = 0;
    slop = 5    # allow for matching slight color variations
    channels = ['r', 'g', 'b']
    for channel in channels:
        channel_total = 0
        channel_matched = 0
        buckets = 256
        available = [0 for i in range(buckets)]
        for i in range (buckets):
            available[i] = abs(histogram[channel][i] - start[channel][i])
        for i in range (buckets):
            target = abs(final[channel][i] - start[channel][i])
            if (target):
                channel_total += target
                low = max(0, i - slop)
                high = min(buckets, i + slop)
                for j in range(low, high):
                    this_match = min(target, available[j])
                    available[j] -= this_match
                    channel_matched += this_match
                    target -= this_match
        total += channel_total
        matched += channel_matched
    progress = (float(matched) / float(total)) if total else 1
    return math.floor(progress * 100)


def find_visually_complete(progress):
    time = 0
    for p in progress:
        if int(p['progress']) == 100:
            time = p['time']
            break
        elif time == 0:
            time = p['time']
    return time


def calculate_speed_index(progress):
    si = 0
    last_ms = progress[0]['time']
    last_progress = progress[0]['progress']
    for p in progress:
        elapsed = p['time'] - last_ms
        si += elapsed * (1.0 - last_progress)
        last_ms = p['time']
        last_progress = p['progress'] / 100.0
    return int(si)


########################################################################################################################
#   Check any dependencies
########################################################################################################################


def check_config():
    ok = True

    print 'ffmpeg:  ',
    if get_decimate_filter() is not None:
        print 'OK'
    else:
        print 'FAIL'
        ok = False

    print 'convert: ',
    if check_process('convert -version', 'ImageMagick'):
        print 'OK'
    else:
        print 'FAIL'
        ok = False

    print 'compare: ',
    if check_process('compare -version', 'ImageMagick'):
        print 'OK'
    else:
        print 'FAIL'
        ok = False

    print 'Pillow:  ',
    try:
        from PIL import Image, ImageDraw
        print 'OK'
    except:
        print 'FAIL'
        ok = False

    return ok


def check_process(command, output):
    ok = False
    try:
        out = subprocess.check_output(command, stderr=subprocess.STDOUT, shell=True)
        if out.find(output) > -1:
            ok = True
    except:
        ok = False
    return ok


########################################################################################################################
#   Main Entry Point
########################################################################################################################


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Calculate visual performance metrics from a video.',
                                     prog='visualmetrics')
    parser.add_argument('--version', action='version', version='%(prog)s 0.1')
    parser.add_argument('-c', '--check', action='store_true', default=False,
                        help="Check dependencies (ffmpeg, imagemagick, PIL).")
    parser.add_argument('-v', '--verbose', action='count', help="Increase verbosity (specify multiple times for more).")
    parser.add_argument('-i', '--video', help="Input video file.")
    parser.add_argument('-d', '--dir', help="Directory of video frames "
                                            "(as input if exists or as output if a video file is specified).")
    parser.add_argument('-g', '--histogram', help="Histogram file (as input if exists or as output if "
                                                  "histograms need to be calculated).")
    parser.add_argument('-m', '--timeline', help="Timeline capture from Chrome dev tools. Used to synchronize the video"
                                                 " start time and only applies when orange frames are removed "
                                                 "(see --orange). The timeline file can be gzipped if it ends in .gz")
    parser.add_argument('-q', '--quality', type=int, help="JPEG Quality "
                                                          "(if specified, frames will be converted to JPEG).")
    parser.add_argument('-l', '--full', action='store_true', default=False,
                        help="Keep full-resolution images instead of resizing to 400x400 pixels")
    parser.add_argument('-f', '--force', action='store_true', default=False,
                        help="Force processing of a video file (overwrite existing directory).")
    parser.add_argument('-o', '--orange', action='store_true', default=False,
                        help="Remove orange-colored frames from the beginning of the video.")
    parser.add_argument('-p', '--viewport', action='store_true', default=False,
                        help="Locate and use the viewport from the first video frame.")
    parser.add_argument('-s', '--start', type=int, default=0, help="Start time (in milliseconds) for calculating "
                                                                   "visual metrics.")
    parser.add_argument('-e', '--end', type=int, default=0, help="End time (in milliseconds) for calculating "
                                                                 "visual metrics.")
    options = parser.parse_args()

    if not options.check and not options.dir and not options.video and not options.histogram:
        parser.error("A video, Directory of images or histograms file needs to be provided.\n\n"
                     "Use -h to see available options")

    temp_dir = tempfile.mkdtemp(prefix='vis-')
    directory = temp_dir
    if options.dir is not None:
        directory = options.dir
    if options.histogram is not None:
        histogram_file = options.histogram
    else:
        histogram_file = os.path.join(temp_dir, 'histograms.json.gz')

    # Set up logging
    log_level = logging.CRITICAL
    if options.verbose == 1:
        log_level = logging.ERROR
    elif options.verbose == 2:
        log_level = logging.WARNING
    elif options.verbose == 3:
        log_level = logging.INFO
    elif options.verbose >= 4:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level)

    ok = False
    try:
        if not options.check:
            viewport = None
            if options.video:
                orange_file = None
                if options.orange:
                    orange_file = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'orange.png')
                if options.orange and\
                    not os.path.isfile(orange_file):
                    orange_file = os.path.join(temp_dir, 'orange.png')
                    generate_orange_png(orange_file)
                video_to_frames(options.video, directory, options.force, orange_file, options.viewport, options.full,
                                options.timeline)
            calculate_histograms(directory, histogram_file, options.force)
            if options.dir is not None and options.quality is not None:
                convert_to_jpeg(directory, options.quality)
            metrics = calculate_visual_metrics(histogram_file, options.start, options.end)
            if metrics is not None:
                ok = True
                for metric in metrics:
                    print "{0}: {1}".format(metric['name'], metric['value'])
        else:
            ok = check_config()
    except Exception as e:
        logging.exception(e)
        ok = False

    # Clean up
    shutil.rmtree(temp_dir)
    if ok:
        exit(0)
    else:
        exit(1)


if '__main__' == __name__:
    main()
