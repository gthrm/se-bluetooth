# SE Bluetooth

Transfer photos, music, videos and other files between a Mac and a Sony Ericsson
W810i over Bluetooth, with no cable and no Windows-only tools.

It's a small Mac app with a file browser. You can:

- browse the phone memory and the Memory Stick folder by folder;
- see thumbnails of camera photos (read from the photo's EXIF block, about 1 s each);
- drag files onto the phone, with live progress;
- shrink pictures to the 176×220 screen, re-encode music as smaller MP3s and convert
  video to the phone's 3GP format on the way;
- download files (one at a time or as a zip), with progress;
- create folders and delete files.

A command-line tool covers scripting.

Tested with a **Sony Ericsson W810i** (firmware `R4EA031`) and a Memory Stick Micro,
on an Apple Silicon MacBook Pro with macOS 27.

## Requirements

- macOS with [Homebrew](https://brew.sh)
- Python **3.10 or newer**, e.g. `brew install python`
- `ffmpeg`, optional, for music and video conversion: `brew install ffmpeg`
- `opencore-amr`, optional, for video conversion (the phone's video sound is AMR):
  `brew install opencore-amr`

## Install the app

```sh
./make_app.sh
```

This builds `~/Applications/SE Bluetooth.app`, with its own virtualenv (PyObjC
IOBluetooth, Pillow, pillow-heif) inside the bundle. The virtualenv uses whichever
`python3` is first on your `PATH`, so keep that Python installed.

Open the app from Spotlight or Finder. It starts a local server on `127.0.0.1:8766`
and opens the UI in your default browser. The first time, macOS asks whether
SE Bluetooth may use Bluetooth: allow it. Re-run `./make_app.sh` after changing the
code.

## Using the app

1. **Pair the phone (once).**
   - On the phone: *Settings → Connectivity → Bluetooth* → turn it on, then
     *Visibility → Show phone*.
   - In the app click **Find phone**, then **Pair** next to the W810i.
   - When the phone asks for a passcode, enter `0000`. If the Mac asks too, enter
     `0000` there as well.
   - Optional: on the phone, open *Bluetooth → My devices*, pick the Mac and allow it
     to connect without asking.
2. **Browse.** The sidebar has shortcuts to the phone's media folders (*Pictures*,
   *Camera*, *Sounds*, *Videos*, *Themes*, and the same on the Memory Stick) and lists
   the folders of each memory. Click a folder card to open it, or a breadcrumb to go
   back. Files whose names start with a dot (Mac leftovers such as `._photo.jpg`) are
   hidden unless *Show hidden files* is ticked.
3. **Preview.** Camera photos get a thumbnail. Click a picture to load it in full, or
   a 3GP/MP4 clip to play it (the app converts it for the browser; needs ffmpeg).
4. **Phone → Mac.** Select files and click **Download**. One file is saved as it
   is; several files come as `W810i_files.zip`. Both go to your browser's Downloads
   folder.
5. **Mac → phone.** Drag files into the window or click **Upload to phone…**.
   - **Where files go.** With *Put into Pictures / Sounds / Videos by type* ticked
     (the default), pictures, music, videos and themes go to the phone's own folders
     on the memory you have open: `Pictures`, `Sounds`, `Videos`, `Themes` in the phone
     memory, or `MSSEMC/Media files/image`, `audio`, `video`, `theme` on the Memory
     Stick. Anything else goes into the open folder.
   - **Names.** Cyrillic and other non-Latin names are kept. Characters a memory card
     can't store (`\ / : * ? " < > |`) become `_`. Existing files are never
     overwritten: a clashing name gets a suffix (`photo_1.jpg`).
   - **Space.** The phone doesn't report free space over Bluetooth. When it is full,
     the upload stops with "Not enough space on the phone".
6. **Compression on upload.** Choose it in the *When uploading* bar; the choice is
   remembered.
   - **Pictures.** *Fit screen 176×220* shrinks a photo to fit the W810i display;
     *Wallpaper 176×220* scales and crops to fill it. The result is a baseline JPEG
     with the EXIF rotation applied. HEIC, PNG, WebP, TIFF and BMP are converted too.
   - **Music.** *Compress MP3 to N kbps* re-encodes to 44.1 kHz MP3 (mono at 48 kbps
     and below). M4A, AAC, WAV, AIFF, FLAC and OGG become MP3 too. Needs ffmpeg.
   - **Videos.** *Convert to 3GP* makes MPEG-4 176×144 video with AMR sound, the
     format the phone records itself. Needs ffmpeg and opencore-amr.
7. **Quit.** **Quit** stops the app and closes the Bluetooth link. The app also
   closes the link after 5 minutes without phone work, and stops on its own 10 minutes
   after the browser tab is closed.

The app listens only on `127.0.0.1`. It rejects requests with a foreign `Host` or
`Origin`, and every state-changing request must carry an `X-SE` header. This
stops other websites open in the browser from touching the phone.

## Speed

Bluetooth on the W810i is slow:

- **Connecting** takes 2–15 s: the phone answers a Bluetooth page slowly.
- **Reading** from the phone runs at about 15 KB/s: a 220 KB camera photo takes about
  15 s.
- **Writing** to the phone runs at about 25 KB/s: a 3.5 MB MP3 takes about 2½ minutes,
  so compressing music on the way pays off.

The progress bar shows speed and time left.

## Command line

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

./se scan                     # search for phones nearby (make the phone visible first)
./se pair <address> [code]    # pair; enter the same code on the phone (default 0000)
./se ls [folder]              # list a folder, e.g. "/Phone memory/Pictures"
./se tree [folder]            # list every file below a folder
./se get <path> [dest]        # download one file
./se pull <folder> <dir>      # download every file in a folder
./se put <file> <path>        # upload (path ending in / keeps the file name)
    [--fit | --wallpaper]     #   resize pictures to the 176x220 screen
    [--mp3 KBPS]              #   re-encode music as MP3 (128/96/64/48/32)
    [--video small|normal]    #   convert video to 3GP 176x144
./se rm <path>                # delete a file or an empty folder
./se mkdir <path>             # create a folder
```

The CLI talks to the first paired Sony Ericsson phone, or to `SE_PHONE=<address>`.

Example: back up the camera photos from the Memory Stick:

```sh
./se pull "/Memory Stick/DCIM/100MSDCF" ~/Desktop/W810i_photos
```

Example: put a wallpaper and a song on the phone:

```sh
./se put --wallpaper ~/Pictures/IMG_1234.HEIC "/Phone memory/Pictures/"
./se put --mp3 128 ~/Music/song.m4a "/Memory Stick/MSSEMC/Media files/audio/"
```

## Files

| File           | What it is                                                         |
|----------------|--------------------------------------------------------------------|
| `bt.py`        | Bluetooth on macOS (IOBluetooth via PyObjC): scan, pair, RFCOMM    |
| `obex.py`      | OBEX File Transfer client, path-based file access and the CLI      |
| `media.py`     | Picture/MP3/3GP conversion, EXIF thumbnails, video previews        |
| `se_app.py`    | Local HTTP server behind the app                                   |
| `se_ui.html`   | The app's UI                                                       |
| `make_app.sh`  | Builds `~/Applications/SE Bluetooth.app`                           |
| `se`           | CLI wrapper that runs `obex.py` from `.venv`                       |

## How it works

The W810i offers these Bluetooth services (found with an SDP query):

| Service                 | RFCOMM channel |
|-------------------------|----------------|
| OBEX SyncML Client      | 1              |
| Dial-up Networking      | 2              |
| Serial Port             | 3              |
| HF / HS Voice Gateway   | 4 / 5          |
| OBEX Object Push        | 6              |
| **OBEX File Transfer**  | **7**          |
| OBEX IrMC Sync Server   | 8              |

The app looks up the *OBEX File Transfer* channel by name rather than trusting the
number, opens an RFCOMM channel to it with IOBluetooth, and speaks OBEX 1.0 over it:

| Operation       | OBEX request                                                        |
|-----------------|---------------------------------------------------------------------|
| connect         | `CONNECT` with Target = Folder Browsing UUID `F9EC7BC4-953C-11D2-984E-525400DC9E09` |
| change folder   | `SETPATH` (flags `0x02`; empty Name = root; flag `0x01` = parent)    |
| list folder     | `GET` with Type `x-obex/folder-listing` (XML)                        |
| read file       | `GET` with Name; `ABORT` after the first 16 KB for a thumbnail       |
| write file      | `PUT` with Name, Length and Body chunks                              |
| delete          | `PUT` with Name and no body                                          |
| create folder   | `SETPATH` with flags `0x00` (create)                                 |

The phone accepts OBEX packets up to 4 KB. Its file system root holds two folders:
`Phone memory` and `Memory Stick`. The `x-obex/capability` object names the media
folders but has no free-space figures.

IOBluetooth delivers its callbacks through the main thread's run loop, so the app
keeps the main thread for Bluetooth and serves HTTP from other threads.

## Known issues

- If a connection breaks off mid-transfer (the app is killed, the Mac sleeps), the
  phone keeps the old session for up to half a minute and refuses new ones. The app
  says so; wait and click **Refresh**. If the phone stays unreachable, turn its
  Bluetooth off and on.
- Now and then the phone stops answering in the middle of a session. Reads (listings,
  thumbnails, downloads) are retried once on a fresh connection; an upload stops and
  the log says how many files were sent.
- Pictures without an EXIF thumbnail (not taken by a camera) show only their type
  until you open them: reading the whole file just for a thumbnail would be too slow.
- Only tested on the W810i. Other Sony Ericsson phones of the same era (K750i, W800i,
  K800i, …) use the same OBEX folder browsing and should work, but their folder names
  may differ.

## License

GPL-2.0-or-later. `media.py` comes from Moto P2K, which uses the same license.
See [LICENSE](LICENSE).
