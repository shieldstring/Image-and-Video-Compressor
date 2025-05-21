import os
from flask import Flask, request, jsonify
from PIL import Image
import cloudinary
import cloudinary.uploader
import io
import tempfile
import logging
import subprocess 

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Configuration ---
# IMPORTANT: Replace with your actual Cloudinary credentials
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', 'your_cloud_name')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', 'your_api_key')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', 'your_api_secret')

# Configure Cloudinary
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET
)

app = Flask(__name__)

# --- Helper Functions ---

def compress_image(image_file, quality=85):
    """
    Compresses an image using Pillow.
    Args:
        image_file: A file-like object (e.g., from request.files).
        quality: The compression quality (0-100).
    Returns:
        A BytesIO object containing the compressed image, or None if an error occurs.
    """
    try:
        img = Image.open(image_file)
        # Convert to RGB if image has an alpha channel (e.g., PNG) to save as JPG
        if img.mode == 'RGBA':
            img = img.convert('RGB')

        output_buffer = io.BytesIO()
        # Save as JPEG for better compression, specify quality
        img.save(output_buffer, format='JPEG', quality=quality)
        output_buffer.seek(0) # Rewind the buffer to the beginning
        logging.info("Image compressed successfully.")
        return output_buffer
    except Exception as e:
        logging.error(f"Error compressing image: {e}")
        return None

def compress_video(video_file, output_path, crf=28):
    try:
        command = [
            'ffmpeg',
            '-i', video_file,
            '-vcodec', 'libx264',
            '-crf', str(crf),
            '-preset', 'medium', # 'ultrafast', 'superfast', 'fast', 'medium', 'slow', 'slower', 'veryslow
            '-y', # Overwrite output file without asking
            output_path
        ]
        logging.info(f"Attempting video compression with command: {' '.join(command)}")
        subprocess.run(command, check=True, capture_output=True)
        logging.info(f"Video compressed successfully to {output_path}")
        return True
    except FileNotFoundError:
        logging.error("Error: ffmpeg not found. Please install ffmpeg and ensure it's in your system's PATH.")
        return False
    except subprocess.CalledProcessError as e:
        logging.error(f"Error compressing video with ffmpeg: {e.stderr.decode()}")
        return False
    except Exception as e:
        logging.error(f"An unexpected error occurred during video compression: {e}")
        return False

# --- API Endpoint ---

@app.route('/upload-and-compress', methods=['POST'])
def upload_and_compress():
    """
    API endpoint to receive an image or video, compress it, and upload to Cloudinary.
    """
    logging.info("Received upload request.")
    if 'file' not in request.files:
        logging.warning("No file part in the request.")
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files['file']

    if file.filename == '':
        logging.warning("No selected file in the request.")
        return jsonify({"error": "No selected file"}), 400

    if file:
        filename = file.filename
        file_extension = filename.split('.')[-1].lower()
        resource_type = 'image' if file_extension in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else \
                        'video' if file_extension in ['mp4', 'mov', 'avi', 'mkv'] else \
                        None

        if resource_type is None:
            logging.warning(f"Unsupported file type uploaded: {file_extension}")
            return jsonify({"error": "Unsupported file type"}), 400

        try:
            if resource_type == 'image':
                logging.info(f"Processing image file: {filename}")
                compressed_file_buffer = compress_image(file.stream)
                if compressed_file_buffer is None:
                    logging.error(f"Image compression failed for {filename}.")
                    return jsonify({"error": "Failed to compress image"}), 500

                # Upload compressed image to Cloudinary
                logging.info(f"Uploading compressed image {filename} to Cloudinary.")
                upload_result = cloudinary.uploader.upload(
                    compressed_file_buffer,
                    resource_type='image',
                    folder="compressed_gallery_images", # Optional: folder in Cloudinary
                    quality="auto:eco" # Cloudinary's auto quality optimization
                )
                logging.info(f"Image {filename} uploaded successfully to Cloudinary. URL: {upload_result['secure_url']}")
                return jsonify({
                    "message": "Image compressed and uploaded successfully",
                    "original_filename": filename,
                    "cloudinary_url": upload_result['secure_url'],
                    "public_id": upload_result['public_id']
                }), 200

            elif resource_type == 'video':
                logging.info(f"Processing video file synchronously: {filename}")
                # Save the uploaded video to a temporary file for ffmpeg processing
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_extension}') as temp_input_file:
                    file.save(temp_input_file.name)
                    original_input_path = temp_input_file.name
                logging.info(f"Original video saved to temporary path: {original_input_path}")

                # Define a temporary output path for the compressed video
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'_compressed.mp4') as temp_output_file:
                    compressed_output_path = temp_output_file.name
                logging.info(f"Compressed video will be saved to temporary path: {compressed_output_path}")

                # Compress video using ffmpeg
                compression_successful = compress_video(original_input_path, compressed_output_path)

                # Clean up original temp file
                os.unlink(original_input_path)
                logging.info(f"Cleaned up original temporary video file: {original_input_path}")

                if not compression_successful:
                    # Clean up partially created compressed file if any
                    if os.path.exists(compressed_output_path):
                        os.unlink(compressed_output_path)
                        logging.warning(f"Cleaned up partially created compressed video file: {compressed_output_path}")
                    logging.error(f"Video compression failed for {filename}.")
                    return jsonify({"error": "Failed to compress video (ffmpeg issue)"}), 500

                # Upload compressed video to Cloudinary
                logging.info(f"Uploading compressed video {filename} from {compressed_output_path} to Cloudinary.")
                upload_result = cloudinary.uploader.upload(
                    compressed_output_path,
                    resource_type='video',
                    folder="compressed_gallery_videos", # Optional: folder in Cloudinary
                    quality="auto:eco" # Cloudinary's auto quality optimization for video
                )

                # Clean up compressed temp file
                os.unlink(compressed_output_path)
                logging.info(f"Cleaned up compressed temporary video file: {compressed_output_path}")

                logging.info(f"Video {filename} uploaded successfully to Cloudinary. URL: {upload_result['secure_url']}")
                return jsonify({
                    "message": "Video compressed and uploaded successfully",
                    "original_filename": filename,
                    "cloudinary_url": upload_result['secure_url'],
                    "public_id": upload_result['public_id']
                }), 200

        except Exception as e:
            logging.exception(f"An unhandled error occurred during processing of {filename}.")
            return jsonify({"error": f"An internal server error occurred: {str(e)}"}), 500

    logging.error("Reached end of function without successful processing or explicit error.")
    return jsonify({"error": "Something went wrong"}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
