# How clip-pipeline works

*A plain-language tour. No programming knowledge needed.*

## The idea in one sentence

You give it a long video. It finds the best moments, turns each one into a short vertical clip with captions, and posts the ones you approve to YouTube, Instagram and TikTok on a schedule.

Everything runs on a computer at home. Your phone is the remote control.

<!-- picture 1: the whole journey -->

## Step 1: You hand over a video

There are three ways in:

- paste a link in the phone app,
- upload a file from the phone,
- drop a file into a shared folder on the computer.

The moment a video arrives, work starts. Nobody has to press "go".

## Step 2: It listens before it looks

Watching a two-hour video frame by frame would take forever. Sound is much cheaper to study, and it gives away the good parts: people get louder, laugh, shout or talk faster when something happens.

So the program first scans only the audio and marks the stretches where the energy jumps. These are the *candidates*. A two-hour video might give thirty of them.

**What you learn here:** start with the cheap signal. Do the expensive work only where the cheap signal says it is worth it.

<!-- picture 2: listening for the loud parts -->

## Step 3: It writes down what was said

Each candidate is turned into text by a speech-recognition model called Whisper. It also records *when* every word was spoken, down to a fraction of a second. That timing is what makes the captions appear word by word later.

The program works out the spoken language by itself. A song in another language playing in the background will not fool it, because it measures how much of the speech is in each language and ignores the small shares.

## Step 4: An AI judges each moment

The text of each candidate goes to a language model (Google's Gemini). It reads the moment like an editor would and gives four marks out of 100:

| Mark | The question it answers |
|---|---|
| Hook | Would the first seconds stop someone scrolling? |
| Engagement | Would people keep watching to the end? |
| Value | Is it funny, useful or surprising? |
| Shareability | Would someone send this to a friend? |

It also suggests where the clip should start and end, and writes a short title for the top of the screen. Only clips that score 70 or more move on.

If the free AI allowance runs out, the program tries another model, then another, and finally falls back to simple rules of its own. The queue never stops because one service said no.

<!-- picture 3: the four marks -->

## Step 5: It makes the clip

Long videos are wide. Phones are tall. So the picture has to be reframed:

- **A face is visible:** the frame follows the face.
- **A game with a small webcam corner:** the screen is split, game on top and face below.
- **Nobody on screen:** the whole picture is shown with a blurred copy of itself filling the gaps.

Then the captions are burned in, the title card is added, black bars are cut off, and the volume is evened out so the clip is not quieter or louder than the ones around it in a feed.

<!-- picture 4: wide becomes tall -->

## Step 6: You decide

Finished clips appear in the phone app as a list. For each one you can:

- watch it,
- fix the title, description and tags,
- choose which accounts it goes to,
- approve or reject it.

Nothing is ever posted without your approval.

## Step 7: It posts on a timetable

Each account has a few posting times per day. An approved clip takes the next free time, so clips are spread out instead of all landing at once. When every platform has finished, the video file is deleted to free space. Later the program reads the view counts back so you can see what worked.

<!-- picture 5: clips waiting for their time slot -->

## How the phone and the computer trust each other

When you pair a phone, it creates a secret key that never leaves the phone. The computer keeps only the matching public half. Every request the phone sends is signed with the secret key, like a wax seal on a letter. The computer checks the seal. A copied request cannot be reused, and a lost phone can be cut off without affecting the others.

There is no password stored anywhere to steal.

## Words worth knowing

- **Pipeline:** a chain of steps where each step's result feeds the next one.
- **Transcription:** turning speech into text.
- **Language model:** an AI that reads and writes text.
- **Rendering:** producing the final video file.
- **Queue:** a waiting line. One job is worked on at a time, so the computer is never overloaded.
- **Resumable:** if the computer restarts, the work continues from the last finished step instead of starting over.
