# Image-and-Video-Compressor

Python Flask API endpoint for image and video compression and Cloudinary upload.

Usage:

    Install Dependencies:

    pip install flask pillow cloudinary redis python-dotenv werkzeug

    Install ffmpeg (for video compression):
        Linux: sudo apt update && sudo apt install ffmpeg
        macOS: brew install ffmpeg (if you have Homebrew)
        Windows: Download from the official ffmpeg website and add it to your system's PATH.


    Cloudinary Credentials:
        Sign up for a Cloudinary account if you don't have one.
        Go to your Cloudinary Dashboard to find your CLOUD_NAME, API_KEY, and API_SECRET.
        Replace the placeholder values for CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET in the Python code with your actual credentials. For production, it's highly recommended to use environment variables (e.g., os.environ.get('CLOUDINARY_CLOUD_NAME')).

How to Run:

    Open your terminal or command prompt.
    Navigate to the directory where you saved the file.
    Run the application: python app.py
    The API will be running on http://127.0.0.1:5000.

How to Test (Example using curl):

For Image Upload:
Bash

```
curl -X POST -F "file=@/path/to/your/image.jpg" http://127.0.0.1:5000/upload-and-compress
```

Replace /path/to/your/image.jpg with the actual path to an image file on your system.

For Video Upload (requires ffmpeg installed):
Bash

`curl -X POST -F "file=@/path/to/your/video.mp4" http://127.0.0.1:5000/upload-and-compress`

Replace /path/to/your/video.mp4 with the actual path to a video file on your system.

Response:

```
{
  "message": "Video compression job created",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

Check Job Status

```
curl -H "X-API-Key: your_api_key" \http://localhost:5000/job/550e8400-e29b-41d4-a716-446655440000
```

Stream Job Progress Updates

```
curl -H "X-API-Key: your_api_key" \
  http://localhost:5000/job/550e8400-e29b-41d4-a716-446655440000/stream
```

Check Service Health:

```
curl http://localhost:5000/health
```
