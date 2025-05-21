import os
import time
from flask import Flask, request, jsonify, Response
from PIL import Image
import cloudinary
import cloudinary.uploader
import io
import tempfile
import logging
import subprocess
import threading
import queue
import uuid
import mimetypes
from werkzeug.utils import secure_filename
from functools import wraps
import redis
import json

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
# IMPORTANT: Replace with your actual Cloudinary credentials
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', 'your_cloud_name')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', 'your_api_key')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', 'your_api_secret')

# Redis for job tracking and caching
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.from_url(REDIS_URL)

# File upload settings
MAX_CONTENT_LENGTH = int(os.environ.get('MAX_CONTENT_LENGTH', 100 * 1024 * 1024))  # 100MB max by default
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp'}
ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm'}
ALLOWED_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS.union(ALLOWED_VIDEO_EXTENSIONS)

# Processing queue and settings
video_processing_queue = queue.Queue()
MAX_WORKER_THREADS = int(os.environ.get('MAX_WORKER_THREADS', 5))
JOB_TIMEOUT = int(os.environ.get('JOB_TIMEOUT', 3600))  # 1 hour timeout for jobs

# Configure Cloudinary
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# --- Helper Functions ---

def allowed_file(filename):
    """Check if the file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_file_type(filename):
    """Determine if the file is an image or video based on extension."""
    ext = filename.rsplit('.', 1)[1].lower()
    if ext in ALLOWED_IMAGE_EXTENSIONS:
        return 'image'
    elif ext in ALLOWED_VIDEO_EXTENSIONS:
        return 'video'
    return None

def validate_file_content(file_stream, claimed_type):
    """Validate that the file content matches its claimed type."""
    # Save a copy of the stream content
    content = file_stream.read()
    file_stream.seek(0)  # Reset stream position
    
    # Check magic numbers for common file types
    magic_numbers = {
        # Images
        b'\xFF\xD8\xFF': 'image/jpeg',
        b'\x89PNG\r\n\x1A\n': 'image/png',
        b'GIF87a': 'image/gif',
        b'GIF89a': 'image/gif',
        b'RIFF': 'image/webp',  # WEBP starts with "RIFF"
        
        # Videos
        b'\x00\x00\x00\x18ftypmp42': 'video/mp4',
        b'\x00\x00\x00\x1cftypisom': 'video/mp4',
        b'\x00\x00\x00\x20ftyp': 'video/mp4',
        b'RIFF': 'video/avi',  # AVI also starts with "RIFF"
    }
    
    for signature, mime_type in magic_numbers.items():
        if content.startswith(signature):
            expected_type = 'image' if mime_type.startswith('image') else 'video'
            file_stream.seek(0)  # Reset stream position again
            return expected_type == claimed_type
            
    # If we can't determine the type from magic numbers, we'll trust the extension
    file_stream.seek(0)  # Reset stream position again
    return True

def compress_image(image_file, quality=85, max_dimensions=(1920, 1080)):
    """
    Compresses an image using Pillow with size limits.
    Args:
        image_file: A file-like object (e.g., from request.files).
        quality: The compression quality (0-100).
        max_dimensions: Maximum width and height.
    Returns:
        A BytesIO object containing the compressed image, or None if an error occurs.
    """
    try:
        img = Image.open(image_file)
        
        # Resize if the image is too large
        if img.width > max_dimensions[0] or img.height > max_dimensions[1]:
            img.thumbnail(max_dimensions, Image.LANCZOS)
            logger.info(f"Image resized to {img.width}x{img.height}")
        
        # Convert to RGB if image has an alpha channel (e.g., PNG) to save as JPG
        if img.mode == 'RGBA':
            img = img.convert('RGB')

        output_buffer = io.BytesIO()
        # Save as JPEG for better compression, specify quality
        img.save(output_buffer, format='JPEG', quality=quality)
        output_buffer.seek(0)  # Rewind the buffer to the beginning
        
        # Log compression ratio
        original_size = image_file.tell()
        compressed_size = output_buffer.getbuffer().nbytes
        ratio = (1 - (compressed_size / original_size)) * 100 if original_size > 0 else 0
        logger.info(f"Image compressed from {original_size/1024:.2f}KB to {compressed_size/1024:.2f}KB ({ratio:.2f}% reduction)")
        
        return output_buffer
    except Exception as e:
        logger.error(f"Error compressing image: {e}")
        return None

def compress_video(video_path, output_path, crf=28, preset='medium', max_resolution='1920x1080'):
    """
    Compresses a video using ffmpeg with more control options.
    """
    try:
        # Get video info before compression
        probe_cmd = [
            'ffprobe', 
            '-v', 'error', 
            '-show_entries', 'format=size', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            video_path
        ]
        original_size = int(subprocess.check_output(probe_cmd, stderr=subprocess.STDOUT).decode().strip())
        
        # Compress the video
        command = [
            'ffmpeg',
            '-i', video_path,
            '-vf', f'scale=min(iw\,{max_resolution.split("x")[0]}):min(ih\,{max_resolution.split("x")[1]}):force_original_aspect_ratio=decrease',
            '-vcodec', 'libx264',
            '-crf', str(crf),
            '-preset', preset,
            '-movflags', '+faststart',  # Optimize for web streaming
            '-y',  # Overwrite output file without asking
            output_path
        ]
        logger.info(f"Attempting video compression with command: {' '.join(command)}")
        
        # Run the ffmpeg command and capture progress
        process = subprocess.Popen(
            command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        
        # Process the output to extract progress information
        while True:
            line = process.stdout.readline()
            if not line:
                break
            
            # Example of extracting progress info from ffmpeg output
            if "time=" in line:
                try:
                    time_info = line.split("time=")[1].split()[0]
                    # You could store this progress info in Redis for the job
                    # redis_client.hset(f"job:{job_id}", "progress", time_info)
                    logger.debug(f"Progress: {time_info}")
                except Exception:
                    pass
        
        process.wait()
        
        if process.returncode != 0:
            logger.error(f"ffmpeg returned non-zero exit code: {process.returncode}")
            return False
            
        # Get compressed video info
        if os.path.exists(output_path):
            probe_cmd = [
                'ffprobe', 
                '-v', 'error', 
                '-show_entries', 'format=size', 
                '-of', 'default=noprint_wrappers=1:nokey=1', 
                output_path
            ]
            compressed_size = int(subprocess.check_output(probe_cmd, stderr=subprocess.STDOUT).decode().strip())
            
            ratio = (1 - (compressed_size / original_size)) * 100 if original_size > 0 else 0
            logger.info(f"Video compressed from {original_size/1024/1024:.2f}MB to {compressed_size/1024/1024:.2f}MB ({ratio:.2f}% reduction)")
            return True
        else:
            logger.error(f"Output file not found: {output_path}")
            return False
            
    except FileNotFoundError:
        logger.error("Error: ffmpeg not found. Please install ffmpeg and ensure it's in your system's PATH.")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Error compressing video with ffmpeg: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred during video compression: {e}")
        return False

def process_video_job(job_id, file_path, original_filename):
    """Process a video compression job from the queue."""
    try:
        # Update job status
        redis_client.hset(f"job:{job_id}", "status", "processing")
        
        # Define a temporary output path for the compressed video
        with tempfile.NamedTemporaryFile(delete=False, suffix='_compressed.mp4') as temp_output_file:
            compressed_output_path = temp_output_file.name
        
        # Compress video using ffmpeg
        compression_successful = compress_video(file_path, compressed_output_path)
        
        # Clean up original temp file
        os.unlink(file_path)
        logger.info(f"Cleaned up original temporary video file: {file_path}")
        
        if not compression_successful:
            # Clean up partially created compressed file if any
            if os.path.exists(compressed_output_path):
                os.unlink(compressed_output_path)
            redis_client.hset(f"job:{job_id}", "status", "failed")
            redis_client.hset(f"job:{job_id}", "error", "Video compression failed")
            logger.error(f"Video compression failed for {original_filename}.")
            return
            
        # Upload compressed video to Cloudinary
        logger.info(f"Uploading compressed video {original_filename} to Cloudinary.")
        redis_client.hset(f"job:{job_id}", "status", "uploading")
        
        upload_result = cloudinary.uploader.upload(
            compressed_output_path,
            resource_type='video',
            folder="compressed_gallery_videos",
            quality="auto:eco"
        )
        
        # Clean up compressed temp file
        os.unlink(compressed_output_path)
        logger.info(f"Cleaned up compressed temporary video file: {compressed_output_path}")
        
        # Update job with success and result info
        result = {
            "status": "completed",
            "message": "Video compressed and uploaded successfully",
            "original_filename": original_filename,
            "cloudinary_url": upload_result['secure_url'],
            "public_id": upload_result['public_id'],
            "completed_at": time.time()
        }
        
        redis_client.hmset(f"job:{job_id}", result)
        logger.info(f"Video {original_filename} processed successfully. URL: {upload_result['secure_url']}")
        
        # Set job expiration
        redis_client.expire(f"job:{job_id}", 86400)  # Expire after 24 hours
        
    except Exception as e:
        logger.exception(f"Error processing video job {job_id}: {e}")
        redis_client.hmset(f"job:{job_id}", {
            "status": "failed",
            "error": str(e)
        })
        redis_client.expire(f"job:{job_id}", 86400)  # Expire after 24 hours

def video_worker():
    """Worker thread for processing videos from the queue."""
    while True:
        try:
            job_data = video_processing_queue.get()
            job_id = job_data["job_id"]
            file_path = job_data["file_path"]
            original_filename = job_data["original_filename"]
            
            process_video_job(job_id, file_path, original_filename)
        except Exception as e:
            logger.exception(f"Error in video worker thread: {e}")
        finally:
            video_processing_queue.task_done()

# Start worker threads
for i in range(MAX_WORKER_THREADS):
    t = threading.Thread(target=video_worker, daemon=True)
    t.start()
    logger.info(f"Started worker thread {i+1}")

# --- API Middleware ---

def require_api_key(f):
    """Decorator to require API key for endpoints."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key or api_key != os.environ.get('API_KEY', 'default_dev_key'):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- API Endpoints ---

@app.route('/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    queue_size = video_processing_queue.qsize()
    return jsonify({
        "status": "healthy",
        "queue_size": queue_size,
        "version": "1.1.0"
    })

@app.route('/upload-and-compress', methods=['POST'])
@require_api_key
def upload_and_compress():
    """
    API endpoint to receive an image or video, compress it, and upload to Cloudinary.
    """
    # Validate request
    if 'file' not in request.files:
        logger.warning("No file part in the request.")
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files['file']

    if file.filename == '':
        logger.warning("No selected file in the request.")
        return jsonify({"error": "No selected file"}), 400
        
    if not allowed_file(file.filename):
        logger.warning(f"Unsupported file type: {file.filename}")
        return jsonify({"error": "Unsupported file type"}), 400
    
    # Secure the filename and get file type
    filename = secure_filename(file.filename)
    resource_type = get_file_type(filename)
    
    if not validate_file_content(file, resource_type):
        logger.warning(f"File content validation failed: {filename}")
        return jsonify({"error": "File content doesn't match its extension"}), 400
    
    try:
        if resource_type == 'image':
            # Process image synchronously
            logger.info(f"Processing image file: {filename}")
            compressed_file_buffer = compress_image(file)
            if compressed_file_buffer is None:
                logger.error(f"Image compression failed for {filename}.")
                return jsonify({"error": "Failed to compress image"}), 500

            # Upload compressed image to Cloudinary
            logger.info(f"Uploading compressed image {filename} to Cloudinary.")
            upload_result = cloudinary.uploader.upload(
                compressed_file_buffer,
                resource_type='image',
                folder="compressed_gallery_images",
                quality="auto:eco"
            )
            
            logger.info(f"Image {filename} uploaded successfully to Cloudinary. URL: {upload_result['secure_url']}")
            return jsonify({
                "message": "Image compressed and uploaded successfully",
                "original_filename": filename,
                "cloudinary_url": upload_result['secure_url'],
                "public_id": upload_result['public_id']
            }), 200

        elif resource_type == 'video':
            # Process video asynchronously
            logger.info(f"Creating async job for video file: {filename}")
            job_id = str(uuid.uuid4())
            
            # Save the uploaded video to a temporary file for ffmpeg processing
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{filename.rsplit(".", 1)[1].lower()}') as temp_input_file:
                file.save(temp_input_file.name)
                original_input_path = temp_input_file.name
            logger.info(f"Original video saved to temporary path: {original_input_path}")
            
            # Create and save job info
            job_info = {
                "job_id": job_id,
                "status": "queued",
                "original_filename": filename,
                "created_at": time.time()
            }
            redis_client.hmset(f"job:{job_id}", job_info)
            
            # Add to processing queue
            video_processing_queue.put({
                "job_id": job_id,
                "file_path": original_input_path,
                "original_filename": filename
            })
            
            logger.info(f"Video job {job_id} created and queued for processing")
            return jsonify({
                "message": "Video compression job created",
                "job_id": job_id,
                "status": "queued"
            }), 202

    except Exception as e:
        logger.exception(f"An unhandled error occurred during processing of {filename}.")
        return jsonify({"error": f"An internal server error occurred: {str(e)}"}), 500

    logger.error("Reached end of function without successful processing or explicit error.")
    return jsonify({"error": "Something went wrong"}), 500

@app.route('/job/<job_id>', methods=['GET'])
def check_job_status(job_id):
    """
    Check the status of an asynchronous job.
    """
    job_data = redis_client.hgetall(f"job:{job_id}")
    if not job_data:
        return jsonify({"error": "Job not found"}), 404
        
    # Convert bytes to strings
    result = {k.decode('utf-8'): v.decode('utf-8') for k, v in job_data.items()}
    
    # Return appropriate status code based on job status
    if result.get('status') == 'completed':
        return jsonify(result), 200
    elif result.get('status') == 'failed':
        return jsonify(result), 500
    else:
        return jsonify(result), 202  # Still processing

@app.route('/job/<job_id>/stream', methods=['GET'])
def stream_job_progress(job_id):
    """
    Stream job progress updates using server-sent events.
    """
    def generate():
        last_status = None
        retry_count = 0
        max_retries = 600  # 10 minutes max (at 1 second intervals)
        
        while retry_count < max_retries:
            job_data = redis_client.hgetall(f"job:{job_id}")
            if not job_data:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                break
                
            # Convert bytes to strings
            result = {k.decode('utf-8'): v.decode('utf-8') for k, v in job_data.items()}
            status = result.get('status')
            
            # Only send update if status has changed
            if status != last_status:
                yield f"data: {json.dumps(result)}\n\n"
                last_status = status
                
            # If job completed or failed, we're done
            if status in ['completed', 'failed']:
                break
                
            time.sleep(1)
            retry_count += 1
            
        # Final update if we timed out
        if retry_count >= max_retries:
            yield f"data: {json.dumps({'error': 'Timeout waiting for job completion'})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    # Set default port and debug settings from environment
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    app.run(debug=debug, port=port, threaded=True)