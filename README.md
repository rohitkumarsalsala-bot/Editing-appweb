# Auto Director (bina AI ka smart video editor)

Engine khud analyse karke decide karta hai: video type (talk/action/visual/general), kitna cut,
kahan zoom, awaaz ki safai + loudness, background blur, colour, fades, music ducking.

## Features
Auto analyse (talk/action/visual) - smart cuts - zoom punch-ins - noise saaf + loudness - blur background
Style looks (cinematic/vivid/warm/vintage/bw) - beat-sync cuts (music do) - transitions - logo overlay
Captions (.srt do, cuts ke hisaab se time khud adjust) - stabilize - music ducking - fades
Note: auto-captions (bolne se text) ke liye speech-recognition chahiye, isliye .srt khud dena hota hai.

## Naye features (v3)
Smart speed (best moment slow-mo, dull hissa 1.6x) - Intro/Outro title cards - Progress bar - Enhance (noise saaf)
Quality: Draft 720p / Standard 1080p / High - Logo + text position - Auto thumbnail - Share/Save button - Presets
Security: ACCESS_KEY env set karo to sirf key wale log use kar sakenge. /health endpoint bhi hai.
Extra: is version mein whoosh sound effects, subject tracking, auto-captions (speech-to-text) NAHI hain - ye AI maangte hain.

## AI features (v4) - optional
- Auto captions (Whisper): bolne se text, Hindi/English/Urdu... auto pehchan, cuts ke hisaab se time sahi
- Filler words hatana (um, uh, hmm) - talking videos mein
- Subject tracking reframe (OpenCV): landscape se 9:16 banate waqt crop face/movement ke saath smooth pan karta hai
- AI title + description + hashtags (Claude API)
Install:  pip install -r requirements.txt -r requirements-ai.txt
Env:      WHISPER_MODEL=small (tiny/base = kam RAM, small = behtar Hindi)  |  ANTHROPIC_API_KEY=...  |  ACCESS_KEY=... (security)
Note: pehli baar Whisper model internet se download hota hai. Free 512MB hosting par small model nahi chalega -
PC / VPS (2GB+ RAM) ya WHISPER_MODEL=tiny/base use karo. Termux par AI libraries install karna mushkil hai.
AI library na ho to app phir bhi chalta hai, bas AI wale options band ho jaate hain (/health se dikhta hai).
Hindi captions Devanagari mein aate hain (Docker image mein Noto font hai).

## Phone par (Termux) chalana
    pkg install python ffmpeg
    pip install flask
    python server.py
Phir browser mein kholo:  http://localhost:8000   (index.html ko file ki tarah mat kholo!)

## Public karna
GitHub par folder daalo -> Render.com / Railway.app -> New Web Service -> Docker -> Deploy.
Env: MAX_MB (upload limit, default 800), WORKERS, KEEP_SECONDS.
