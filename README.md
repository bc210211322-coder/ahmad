# YouTube long gaming video auto poster

Put your clips in a folder, fill in one small file, and this tool joins the clips,
evens out the sound, writes chapters, makes a thumbnail, uploads the video and
schedules it (Wednesday and Saturday, 7 pm Pakistan time).

You still do: record, cut dull parts, write the title and summary.

## One-time setup (Windows)

### 1. Install the programs
Open Command Prompt:

    winget install Python.Python.3.12
    winget install Gyan.FFmpeg

Close and reopen Command Prompt.

### 2. Download this project and install libraries
In the project folder:

    python -m pip install -r requirements.txt

### 3. Get your YouTube key
1. Open https://console.cloud.google.com and create a project.
2. APIs & Services > Library > enable **YouTube Data API v3**.
3. Set up the OAuth consent screen (also called Google Auth Platform): user type **External**.
4. Set the publishing status to **In production**. In "Testing", your sign-in expires every 7 days.
5. Create an **OAuth client ID** of type **Desktop app**, download the JSON and save it
   in this folder as `client_secret.json`.

### 4. Sign in

    python autopost.py auth

Choose your channel. If Google warns that the app is not verified, it is your own
app: use "Advanced" to continue.

Also verify your YouTube account by phone, or videos over 15 minutes and custom
thumbnails will be refused.

## For every video

1. Make a folder inside `videos`, for example `videos/part-01`.
2. Copy your clips in. Name them in order; the names become chapters:
   `01 Intro.mp4`, `02 First Boss.mp4`, `03 Ending.mp4`
3. Copy `videos/example-video/video.json` into your folder and edit it.
4. Set `"ready": true` only after all clips have finished copying.
5. Check it first (nothing is uploaded):

       python autopost.py preview

6. Upload and schedule:

       python autopost.py run

Optional: add your own `thumbnail.jpg` to the folder.

## Make it fully automatic

1. Press Start, open **Task Scheduler**, choose **Create Basic Task**.
2. Trigger: **Daily**, at a time when your PC is on.
3. Action: **Start a program**, and select `run.bat` in this folder.

On Mac or Linux, add a cron job: `0 14 * * * cd /path/to/project && python3 autopost.py run`

Each run uploads every folder marked ready. Once uploaded, YouTube publishes on
schedule even if your PC is off.

## Settings
Edit `config.json` for publishing days and time, video size and frame rate.
- `audio_tracks`: `"first"` uses the first audio track. `"mix"` combines all tracks;
  use it only if game and voice are on separate tracks with no combined track.
- `contains_synthetic_media`: set `true` if a video has realistic altered or AI-made content.
- `made_for_kids`: must truthfully describe your videos.

## Good to know
- Uploaded folders are recorded in `state.json`. Delete an entry only if you want
  that folder uploaded again.
- Deleting a video in YouTube Studio does not free its slot until you remove the entry.
- Problems are written to `autopost.log`.
- Processing long videos takes time and free disk space: allow several times the
  size of your clips.
- **This repository is public. Never upload `client_secret.json` or `token.json`.**
