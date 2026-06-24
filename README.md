# AI Music YouTube Automation

Hệ thống này tự động lấy MP3 + ảnh, tạo video MP4 bằng ffmpeg, tạo metadata theo template, rồi upload/lên lịch YouTube bằng YouTube Data API.

## Cách dùng nhanh

1. Khởi tạo cấu hình:

```powershell
.\run.ps1 init
```

2. Bỏ file vào các thư mục:

- MP3: `data/input/audio`
- Ảnh nền: `data/input/images`
- Thumbnail riêng nếu có: `data/input/thumbnails`

Nếu ảnh có cùng tên với MP3, hệ thống sẽ ghép đúng cặp. Nếu không, hệ thống tự xoay vòng ảnh.

3. Render thử video:

```powershell
.\run.ps1 render -Limit 1
```

4. Chạy thử lịch upload nhưng chưa upload thật:

```powershell
.\run.ps1 daily -DryRun
```

5. Upload/lên lịch thật:

```powershell
.\run.ps1 daily
```

Mặc định `daily` xử lý 2 video mới và lên lịch theo `06:00`, `18:00` trong múi giờ `Asia/Ho_Chi_Minh`.

Khi upload thật, hệ thống sẽ kiểm tra YouTube của account đang chọn. Nếu ngày đó đã có đủ 2 video trên YouTube, kể cả video bạn tự upload thủ công, hệ thống sẽ bỏ qua ngày đó và chuyển sang ngày kế tiếp. Tính năng này cần quyền `youtube.readonly`, nên token cũ có thể cần đăng nhập/cấp quyền lại một lần.

## Giao diện web local

Chạy:

```powershell
.\run_gui.ps1
```

Sau đó mở:

```text
http://127.0.0.1:8000
```

Trong giao diện bạn có thể upload MP3/ảnh, xem track đang chờ, render video, dry-run lịch đăng, schedule upload thật, mở thư mục input/output và sửa `config.json`.

Nếu đang có job upload/render chạy, bạn vẫn có thể thêm file và bấm thao tác tiếp. Job mới sẽ vào hàng đợi và tự chạy sau khi job hiện tại hoàn tất.

Phần `Story Voice` dùng Edge TTS miễn phí để tạo MP3 từ text truyện. MP3 được lưu vào `data/input/audio` và có thể render/upload bằng pipeline hiện tại.

## Tạo token cho tài khoản YouTube thứ hai

Chạy:

```powershell
.\login_account2.ps1
```

Đăng nhập bằng tài khoản Google/YouTube khác. Hệ thống sẽ tạo thêm file:

```text
token_account2.json
```

Token cũ `token.json` vẫn được giữ nguyên. Muốn dùng tài khoản thứ hai để upload, sửa `config.json`:

```json
"token_file": "token_account2.json"
```

Muốn quay lại tài khoản đầu, đổi lại:

```json
"token_file": "token.json"
```

Tài khoản `nhahn3188` dùng file:

```text
token_nhahn3188.json
```

Tạo token bằng:

```powershell
.\login_nhahn3188.ps1
```

Hệ thống đang bật cả video thường và Shorts. Với mỗi MP3 + ảnh, nó tạo:

- Video thường: `1920x1080`, full bài.
- Shorts: `1080x1920`, tối đa 59 giây, thêm `#Shorts`, đăng lệch 30 phút sau video thường.

Khi có đủ 5 video thường đã render, giao diện sẽ đề xuất tạo tuyển tập. Bấm `Create Collection` để ghép 5 video đó thành một file MP4 tổng hợp trong `data/output`.

## Bật upload YouTube

Bạn cần làm một lần:

1. Vào Google Cloud Console.
2. Tạo project mới.
3. Enable **YouTube Data API v3**.
4. Tạo OAuth Client dạng **Desktop app**.
5. Tải file JSON về và đặt tên là `client_secret.json` trong thư mục project này.
6. Chạy:

```powershell
.\run.ps1 daily -DryRun
.\run.ps1 daily
```

Lần upload thật đầu tiên sẽ mở trình duyệt để bạn đăng nhập Google và cấp quyền. Sau đó hệ thống lưu `token.json` để dùng lại.

## Tùy chỉnh

Copy `config.example.json` thành `config.json` nếu chưa có, rồi chỉnh:

- `default_title_template`: mẫu tiêu đề.
- `title_templates`: nhiều mẫu tiêu đề để hệ thống thay phiên theo từng bài.
- `default_description_template`: mẫu mô tả.
- `default_tags`: tags mặc định.
- `privacy_status`: `private`, `unlisted`, hoặc `public`.
- `publish_times`: giờ đăng mỗi ngày.
- `videos_per_day`: số video/ngày.
- `resolution`: ví dụ `1920x1080`.
- `zoom_effect`: bật/tắt hiệu ứng zoom chậm.
- `shorts.enabled`: bật/tắt tạo và upload Shorts.
- `shorts.title_templates`: nhiều mẫu tiêu đề riêng cho Shorts.
- `shorts.max_duration_seconds`: độ dài tối đa của Shorts.
- `shorts.publish_offset_minutes`: Shorts đăng sau video thường bao nhiêu phút.
- `collection.size`: số video thường cần có để tạo tuyển tập, mặc định là 5.
- `collection.output_prefix`: tiền tố tên file tuyển tập.

## Tự động chạy mỗi ngày trên Windows

Mở Task Scheduler và tạo task:

- Program: `powershell.exe`
- Arguments:

```powershell
-ExecutionPolicy Bypass -File "C:\Users\User\Downloads\automation\run.ps1" daily
```

- Start in:

```text
C:\Users\User\Downloads\automation
```

Nên đặt chạy 1 lần/ngày, ví dụ 08:00 sáng. Hệ thống sẽ tự lấy 3 MP3 mới, tạo video và đặt lịch đăng.

## Ghi chú quan trọng

- Pipeline này chưa tự tạo nhạc AI. Nó xử lý nhạc bạn đã có sẵn.
- YouTube có giới hạn quota API và chính sách chống nội dung spam/trùng lặp.
- Nên dùng title, thumbnail, mô tả và hình ảnh khác nhau để kênh nhìn tự nhiên hơn.



## Run Project
cd D:\automation
.\run_gui.ps1
## Run Ngrok
cd D:\automation
powershell -ExecutionPolicy Bypass -File .\run_ngrok_tunnel.ps1
