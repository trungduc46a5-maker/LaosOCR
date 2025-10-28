from flask import Flask, render_template, request, jsonify, send_file
import os
import base64
import requests
import fitz  # PyMuPDF
from PIL import Image
import io
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import json

load_dotenv()
API_KEY = os.getenv('GOOGLE_API_KEY')

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # Max 50MB
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

# Tạo thư mục uploads nếu chưa có
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ===== DATABASE SETUP =====
def init_db():
    """Khởi tạo database"""
    conn = sqlite3.connect('ocr_history.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  filename TEXT NOT NULL,
                  file_type TEXT NOT NULL,
                  total_pages INTEGER,
                  full_text TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

init_db()

def save_to_history(filename, file_type, total_pages, full_text):
    """Lưu kết quả vào database"""
    try:
        conn = sqlite3.connect('ocr_history.db')
        c = conn.cursor()
        c.execute("""INSERT INTO history (filename, file_type, total_pages, full_text) 
                     VALUES (?, ?, ?, ?)""",
                  (filename, file_type, total_pages, full_text))
        conn.commit()
        history_id = c.lastrowid
        conn.close()
        return history_id
    except Exception as e:
        print(f"Error saving to history: {e}")
        return None

def get_history(limit=20):
    """Lấy lịch sử xử lý"""
    try:
        conn = sqlite3.connect('ocr_history.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT * FROM history 
                     ORDER BY created_at DESC LIMIT ?""", (limit,))
        rows = c.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error getting history: {e}")
        return []

def allowed_file(filename):
    """Kiểm tra file có hợp lệ không"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# ===== ROUTES =====
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload-multiple', methods=['POST'])
def upload_multiple():
    """Xử lý nhiều file cùng lúc"""
    if 'files[]' not in request.files:
        return jsonify({'error': 'Không có file'}), 400
    
    files = request.files.getlist('files[]')
    
    if not files or files[0].filename == '':
        return jsonify({'error': 'Chưa chọn file'}), 400
    
    all_results = []
    
    for file in files:
        if file and allowed_file(file.filename):
            try:
                # Tạo tên file unique
                import time
                filename = f"{int(time.time() * 1000)}_{file.filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                
                # Xử lý file dựa trên loại
                file_ext = file.filename.rsplit('.', 1)[1].lower()
                
                if file_ext == 'pdf':
                    result = process_pdf(filepath)
                    result['filename'] = file.filename
                    result['file_type'] = 'PDF'
                else:
                    result = process_image(filepath)
                    result['filename'] = file.filename
                    result['file_type'] = 'Image'
                
                # Lưu vào database
                full_text = '\n\n'.join([page['text'] for page in result.get('pages', [])])
                history_id = save_to_history(
                    file.filename,
                    result['file_type'],
                    result.get('total_pages', 1),
                    full_text
                )
                result['history_id'] = history_id
                
                all_results.append(result)
                
                # Xóa file sau khi xử lý (tùy chọn)
                # os.remove(filepath)
                
            except Exception as e:
                all_results.append({
                    'filename': file.filename,
                    'error': str(e),
                    'file_type': 'Unknown'
                })
        else:
            all_results.append({
                'filename': file.filename,
                'error': 'Định dạng file không được hỗ trợ',
                'file_type': 'Unknown'
            })
    
    return jsonify({'results': all_results})

def process_pdf(pdf_path):
    """Xử lý PDF nhiều trang"""
    results = []
    
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        
        for page_num in range(total_pages):
            try:
                page = doc[page_num]
                
                # Chuyển trang PDF thành ảnh
                zoom = 2
                mat = fitz.Matrix(zoom, zoom)
                pix = page.get_pixmap(matrix=mat)
                img_data = pix.tobytes("png")
                img_base64 = base64.b64encode(img_data).decode('utf-8')
                
                # OCR
                text = ocr_image(img_base64)
                
                results.append({
                    'page': page_num + 1,
                    'text': text,
                    'preview': f"data:image/png;base64,{img_base64}"
                })
                
                pix = None
                
            except Exception as e:
                results.append({
                    'page': page_num + 1,
                    'text': f"Lỗi xử lý trang: {str(e)}",
                    'preview': ''
                })
        
        doc.close()
        return {'pages': results, 'total_pages': total_pages}
        
    except Exception as e:
        return {'error': f"Lỗi xử lý PDF: {str(e)}", 'pages': [], 'total_pages': 0}

def process_image(image_path):
    """Xử lý file ảnh đơn"""
    try:
        # Đọc ảnh và chuyển sang base64
        with open(image_path, 'rb') as img_file:
            img_data = img_file.read()
            img_base64 = base64.b64encode(img_data).decode('utf-8')
        
        # Lấy định dạng ảnh
        img = Image.open(image_path)
        img_format = img.format.lower()
        
        # OCR
        text = ocr_image(img_base64)
        
        return {
            'pages': [{
                'page': 1,
                'text': text,
                'preview': f"data:image/{img_format};base64,{img_base64}"
            }],
            'total_pages': 1
        }
        
    except Exception as e:
        return {
            'error': f"Lỗi xử lý ảnh: {str(e)}",
            'pages': [],
            'total_pages': 0
        }

def ocr_image(image_base64):
    """Gọi Cloud Vision API để OCR"""
    url = f"https://vision.googleapis.com/v1/images:annotate?key={API_KEY}"
    
    request_body = {
        "requests": [{
            "image": {"content": image_base64},
            "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            "imageContext": {
                "languageHints": ["lo", "en", "vi"]  # Lào, Tiếng Anh, Tiếng Việt
            }
        }]
    }
    
    try:
        response = requests.post(url, json=request_body, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            if 'textAnnotations' in result['responses'][0]:
                return result['responses'][0]['textAnnotations'][0]['description']
        
        return "Không nhận diện được text"
    except Exception as e:
        return f"Lỗi OCR: {str(e)}"

@app.route('/history')
def history():
    """Lấy lịch sử xử lý"""
    limit = request.args.get('limit', 20, type=int)
    history_data = get_history(limit)
    return jsonify({'history': history_data})

@app.route('/history/<int:history_id>')
def get_history_detail(history_id):
    """Lấy chi tiết một bản ghi lịch sử"""
    try:
        conn = sqlite3.connect('ocr_history.db')
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM history WHERE id = ?", (history_id,))
        row = c.fetchone()
        conn.close()
        
        if row:
            return jsonify(dict(row))
        else:
            return jsonify({'error': 'Không tìm thấy'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete-history/<int:history_id>', methods=['DELETE'])
def delete_history(history_id):
    """Xóa một bản ghi lịch sử"""
    try:
        conn = sqlite3.connect('ocr_history.db')
        c = conn.cursor()
        c.execute("DELETE FROM history WHERE id = ?", (history_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)