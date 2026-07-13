# AI Music YouTube Automation

Hệ thống này tự động lấy MP3 + ảnh, tạo video MP4 bằng ffmpeg, tạo metadata theo template, rồi upload/lên lịch YouTube bằng YouTube Data API.

## Tổng quan pipeline

Project có bốn nhóm chức năng chính. Mọi thao tác từ giao diện đều tạo `job`, chạy tuần tự qua hàng đợi và ghi log theo từng bước.

| Pipeline | Đầu vào | Đầu ra chính |
| --- | --- | --- |
| MP3/Video thường | MP3, ảnh, thumbnail tùy chọn | Video ngang, Shorts, metadata và lịch YouTube |
| Phật pháp Shorts | Prompt pool, ảnh dọc, kênh đích | Short 9:16 có voice, ảnh, thumbnail và metadata |
| Phật pháp 20-Min/Long | Prompt theo kênh, ảnh ngang và assets dùng chung | Video dài 1080p có chương, subtitle, ambient/effect và upload |
| Sleepu Stories | Title/prompt tùy chọn, số phút và số ảnh | Bedtime story tiếng Anh hoàn toàn mới, ảnh watercolor, voice, thumbnail và upload |

### 1. MP3 và video thường

Luồng cơ bản:

```text
MP3 + ảnh
-> ghép track
-> tạo metadata
-> render video ngang 1920x1080
-> tạo Short 1080x1920 nếu bật
-> kiểm tra lịch và quota
-> upload hoặc lên lịch YouTube
```

- Ảnh trùng tên MP3 được ưu tiên; nếu không có, hệ thống xoay vòng image pool.
- Có thể render, rerender, dry-run, upload video thường, upload Short, bỏ qua hoặc xóa từng track.
- `Sync State` đối chiếu trạng thái local với YouTube để loại record đã mất hoặc video đã bị xóa.
- Collection ghép các video thường đã render thành một video tổng hợp.

### 2. Full Auto Phật pháp Shorts

Luồng tự động:

```text
Chọn prompt tiếp theo trong prompt pool
-> Gemma qua Ollama viết title, hook, script và prompt ảnh
-> kiểm tra định dạng/nội dung
-> tạo voice theo cấu hình từng kênh
-> chọn 5 ảnh dọc
-> render Short có subtitle/effect
-> tạo thumbnail + description/hashtag
-> upload theo lịch riêng của kênh
```

- Prompt dùng chung nằm tại `data/input/buddhist/shared/story-shorts/prompts`.
- Draft nằm tại `data/input/buddhist/shared/story-shorts/drafts`.
- Prompt được tách thành các block `PROMPT N` và xoay vòng theo state; không chọn ngẫu nhiên hoàn toàn.
- Mỗi kênh có token, giọng đọc, image pool, lịch đăng và state upload riêng.
- Job Short chạy qua queue; bấm nhiều lần sẽ xếp hàng thay vì chạy chồng tài nguyên.

### 3. Phật pháp 20-Min

```text
Prompt chủ đề
-> tạo dàn ý nhiều chương
-> viết nội dung từng chương
-> kiểm tra độ dài và trùng lặp
-> tạo một file voice
-> ghép ảnh ngang
-> subtitle + low bed + ambient/effect + sticker
-> render 1080p
-> metadata, timeline, hashtag
-> upload/lên lịch
```

- Mặc định hiện tại khoảng 25 phút, 6 chương và 5 ảnh ngang; tất cả có thể đổi trong `config.json`.
- Hook tiếng Việt được chọn theo biến thể nội dung để hạn chế video mở đầu giống nhau.
- Nội dung, giọng và lịch được tách theo kênh; thay lịch một kênh không làm đổi lịch các kênh còn lại.
- Low bed và ambience được trộn ở âm lượng thấp dưới voice; subtitle nhỏ hơn video Shorts.

### 4. Phật pháp Long

```text
Prompt riêng của kênh
-> tạo outline 18 chương
-> viết từng chương khoảng 1.000 từ
-> tự nối phần còn thiếu bằng nội dung mới
-> chống lặp trong chương và giữa các chương
-> lưu checkpoint sau từng chương
-> tạo voice dài
-> ghép ảnh/effect/ambient/subtitle
-> render 1080p
-> tạo title, description, timeline và hashtag
-> upload/lên lịch
```

- Không chèn văn mẫu lặp chỉ để đủ số từ. Nếu Gemma trả chương ngắn, pipeline yêu cầu viết nối theo đúng chủ đề.
- Prompt continuation được rút gọn để phù hợp context của Gemma local mà vẫn giữ chuẩn độ dài.
- Nếu backend dừng giữa chừng, job được đánh dấu `interrupted`; nút `Resume` dùng checkpoint để tiếp tục thay vì viết lại toàn bộ.
- Nội dung từng kênh nằm trong `data/input/buddhist/channels/<channel>/fullauto-long`.
- Ảnh, effect, wave và sticker dùng chung được đặt trong `data/input/buddhist/shared/long-assets`.

### 5. Gộp video Phật pháp

Giao diện hỗ trợ hai cách:

- Gộp nhóm video 20-Min đủ điều kiện thành video dài hơn.
- Chọn thủ công các video Long đã render rồi bấm `Merge Selected` hoặc `Merge + Upload Selected`.

Việc gộp ưu tiên FFmpeg concat nên nhanh hơn render lại khi codec tương thích. Với luồng gộp đã chọn, video nguồn chỉ được xóa sau khi gộp thành công; file đã gộp chỉ được xóa sau khi upload thành công theo cấu hình dọn file.

### 6. Sleepu Stories Auto Agent

Đây là pipeline riêng cho kênh `Sleepu Stories`; không cho chọn nhầm sang kênh Phật pháp.

```text
Title/prompt hoặc Auto Topic
-> Story Planner
-> Story Writer
-> Reviewer + hard content gate
-> tối đa 2 vòng rewrite
-> Character/World Visual Bible
-> Scene Planner + Prompt Optimizer
-> metadata + thumbnail prompt
-> dỡ Gemma khỏi RAM
-> tạo voice Kokoro
-> ComfyUI tạo ảnh watercolor mới
-> CLIP chọn ảnh tốt nhất
-> tạo thumbnail
-> Speech QA + Content/Image QA
-> render 1080p có subtitle, ambient, low bed và effect
-> Final Media QA
-> upload/lên lịch YouTube
```

Các ràng buộc chất lượng chính:

- Mỗi truyện phải có hook cho người lớn, vấn đề cảm xúc cụ thể, bí ẩn nhỏ, ký ức, hành động, payoff và đoạn kết dẫn vào giấc ngủ.
- Hard gate chặn truyện lặp từ không khí quá nhiều, kết thúc sớm, thiếu mystery payoff, thiếu ký ức cụ thể hoặc quá ít hành động.
- Reviewer dùng JSON mode và context riêng; Writer mới là agent chịu trách nhiệm viết lại.
- Gemma được dỡ khỏi RAM trước ComfyUI để phù hợp laptop 16 GB.
- Voice được tạo trước ảnh để lỗi TTS xuất hiện sớm; ComfyUI tự retry các cảnh còn thiếu.
- Mỗi cảnh mặc định có 2 ảnh ứng viên; CLIP chọn ảnh phù hợp prompt hơn.
- `strict_story_images` yêu cầu đủ ảnh mới của đúng truyện, không dùng placeholder hoặc ảnh reference cũ.
- Thumbnail lỗi không làm mất toàn bộ video; QA voice, subtitle và file MP4 vẫn phải đạt trước upload.

Mặc định cân bằng cho máy hiện tại là 15 phút, 12 ảnh, `1920x1080`, 24 fps. Giao diện vẫn cho chọn video tối đa 30 phút.

### 7. Job, queue và resume

- Chỉ một job nặng chạy tại một thời điểm; job mới hiển thị `queued` và tự chạy sau.
- Trạng thái gồm `queued`, `running`, `done`, `failed`, `interrupted`.
- Log hiển thị stage, tiến độ, thời gian chạy và lỗi gần nhất trên PC lẫn mobile.
- Job Long và Sleep Story bị `interrupted` có thể hiện nút `Resume` nếu tìm thấy checkpoint hợp lệ.
- Không restart backend khi có job `running` hoặc `queued`; nếu bắt buộc restart, checkpoint được giữ để tránh upload trùng.
- Email được gửi cho từng job thành công/thất bại và thêm một email khi toàn bộ queue hoàn tất.

### 8. Upload, tài khoản và lịch

- Mỗi kênh dùng token/state/lịch riêng; đổi kênh đích không làm thay đổi dữ liệu của kênh khác.
- Trước upload, hệ thống kiểm tra file video, audio, metadata, lịch trống và giới hạn video/ngày.
- Upload thành công được ghi `youtube_id`, URL và thời gian publish vào state/draft.
- Nếu token hết hạn hoặc đổi scope, giao diện sẽ yêu cầu đăng nhập lại đúng tài khoản YouTube.
- YouTube API quota, mạng, quyền OAuth và trạng thái xử lý của YouTube vẫn là phụ thuộc bên ngoài pipeline.

### 9. Giao diện PC và mobile

- PC: `http://127.0.0.1:8000` sau khi chạy `run_gui.ps1`.
- Mobile/PWA cung cấp các thao tác Full Auto chính, Sleepu Stories, xem job và resume.
- Khi dùng Tailscale, điện thoại chỉ điều khiển giao diện; mọi AI, TTS, ComfyUI, FFmpeg và upload vẫn chạy trên laptop/server.
- Laptop phải bật, backend phải chạy và không được sleep thì điện thoại mới điều khiển được từ bên ngoài.

### 10. Analytics và vòng lặp tự cải thiện

Nút `Sync Analytics` kết hợp hai nguồn dữ liệu:

- YouTube Analytics API: views, likes, comments, subscribers gained, watch time, average view duration và average view percentage.
- YouTube Reporting API: `video_thumbnail_impressions` và `video_thumbnail_impressions_ctr` từ report `channel_reach_basic_a1`.

Collector tạo reporting job `channel_basic_a3` và `channel_reach_basic_a1`, tải CSV hằng ngày, gộp theo `video_id` và tạo các cửa sổ `metrics_24h`, `metrics_72h`, `metrics_7d`. CTR nhiều ngày được tính theo trọng số impressions, không lấy trung bình đơn giản.

Dữ liệu được lưu tại `data/research/youtube/reporting/<account>` và được View Optimizer dùng để chấm title/thumbnail/retention. Reporting API phải được bật trong Google Cloud. Report đầu tiên có thể mất tới 48 giờ và chỉ bắt đầu từ ngày reporting job được tạo; Sync Analytics cũ vẫn hoạt động nếu Reporting API chưa sẵn sàng.

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

## Tat cong 8000
$portPid = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -First 1); if ($portPid) { Stop-Process -Id $portPid -Force }
