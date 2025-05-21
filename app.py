import os
from flask import Flask, request, jsonify
from PIL import Image
import cloudinary
import cloudinary.uploader
import io
import tempfile

# --- Configuration ---
# IMPORTANT: Replace with your actual Cloudinary credentials
# It's recommended to use environment variables for production
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
        return output_buffer
    except Exception as e:
        print(f"Error compressing image: {e}")
        return None

def compress_video(video_file, output_path, crf=28):
    """
    Compresses a video using ffmpeg.
    NOTE: This function requires ffmpeg to be installed and accessible in the system's PATH.
    It's a placeholder for how you would integrate ffmpeg.
    For a production environment, consider using a dedicated video processing service
    or a more robust ffmpeg wrapper.

    Args:
        video_file: Path to the input video file.
        output_path: Path where the compressed video will be saved.
        crf: Constant Rate Factor for H.264 encoding (lower is higher quality, larger file size).
             Typical values are 18-28. 23 is default, 28 is good for significant compression.
    Returns:
        True if compression was successful, False otherwise.
    """
    # This part needs ffmpeg installed on the server where this API runs.
    # Example command: ffmpeg -i input.mp4 -vcodec libx264 -crf 28 output.mp4
    import subprocess
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
        subprocess.run(command, check=True, capture_output=True)
        return True
    except FileNotFoundError:
        print("Error: ffmpeg not found. Please install ffmpeg and ensure it's in your system's PATH.")
        return False
    except subprocess.CalledProcessError as e:
        print(f"Error compressing video with ffmpeg: {e.stderr.decode()}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred during video compression: {e}")
        return False

# --- API Endpoint ---

@app.route('/upload-and-compress', methods=['POST'])
def upload_and_compress():
    """
    API endpoint to receive an image or video, compress it, and upload to Cloudinary.
    """
    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    if file:
        filename = file.filename
        file_extension = filename.split('.')[-1].lower()
        resource_type = 'image' if file_extension in ['jpg', 'jpeg', 'png', 'gif', 'webp'] else \
                        'video' if file_extension in ['mp4', 'mov', 'avi', 'mkv'] else \
                        None

        if resource_type is None:
            return jsonify({"error": "Unsupported file type"}), 400

        try:
            if resource_type == 'image':
                # Compress image
                compressed_file_buffer = compress_image(file.stream)
                if compressed_file_buffer is None:
                    return jsonify({"error": "Failed to compress image"}), 500

                # Upload compressed image to Cloudinary
                upload_result = cloudinary.uploader.upload(
                    compressed_file_buffer,
                    resource_type='image',
                    folder="compressed_gallery_images", # Optional: folder in Cloudinary
                    quality="auto:eco" # Cloudinary's auto quality optimization
                )
                return jsonify({
                    "message": "Image compressed and uploaded successfully",
                    "original_filename": filename,
                    "cloudinary_url": upload_result['secure_url'],
                    "public_id": upload_result['public_id']
                }), 200

            elif resource_type == 'video':
                # Save the uploaded video to a temporary file for ffmpeg processing
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_extension}') as temp_input_file:
                    file.save(temp_input_file.name)
                    original_input_path = temp_input_file.name

                # Define a temporary output path for the compressed video
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'_compressed.mp4') as temp_output_file:
                    compressed_output_path = temp_output_file.name

                # Compress video using ffmpeg
                compression_successful = compress_video(original_input_path, compressed_output_path)

                # Clean up original temp file
                os.unlink(original_input_path)

                if not compression_successful:
                    # Clean up partially created compressed file if any
                    if os.path.exists(compressed_output_path):
                        os.unlink(compressed_output_path)
                    return jsonify({"error": "Failed to compress video (ffmpeg issue)"}), 500

                # Upload compressed video to Cloudinary
                upload_result = cloudinary.uploader.upload(
                    compressed_output_path,
                    resource_type='video',
                    folder="compressed_gallery_videos", # Optional: folder in Cloudinary
                    quality="auto:eco" # Cloudinary's auto quality optimization for video
                )

                # Clean up compressed temp file
                os.unlink(compressed_output_path)

                return jsonify({
                    "message": "Video compressed and uploaded successfully",
                    "original_filename": filename,
                    "cloudinary_url": upload_result['secure_url'],
                    "public_id": upload_result['public_id']
                }), 200

        except Exception as e:
            print(f"An error occurred: {e}")
            return jsonify({"error": f"An internal server error occurred: {str(e)}"}), 500

    return jsonify({"error": "Something went wrong"}), 500

if __name__ == '__main__':
    # For local development, you can run: python your_api_file_name.py
    # In a production environment, use a WSGI server like Gunicorn or uWSGI.
    app.run(debug=True, port=5000)
