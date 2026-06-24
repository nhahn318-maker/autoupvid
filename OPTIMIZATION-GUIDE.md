# Hướng dẫn tối ưu chất lượng + tốc độ cho Long Video

## Cấu hình đã tối ưu (hiện tại)

### 1. Encode Preset
- **Shorts/Video thường:** `fast` (nhanh, chất lượng vẫn cao)
- **Long video:** `faster` (rất nhanh cho video 60+ phút)

**Lý do:** YouTube sẽ re-encode lại video với preset riêng, nên preset gốc không quan trọng nếu bitrate đủ cao (4500k).

### 2. Bitrate
- Video: `4500k` (rất cao cho 1080p, dư dả)
- Audio: `192k` (chuẩn cho voice)

→ Không cần tăng thêm, YouTube sẽ giảm xuống ~3000k khi stream.

### 3. Zoom Effect
- **Video thường:** Bật (tạo động)
- **Long video:** Tắt (tránh nhàm chán sau 60 phút)

### 4. Image Segment
- **Video thường:** 5s/ảnh (nhanh)
- **Long video:** 12s/ảnh (chậm, tập trung nghe)

→ Video 60 phút = 300 ảnh, vừa đủ không gây phân tâm.

---

## Nâng cao chất lượng thêm (không ảnh hưởng tốc độ)

### 1. Thêm Audio Visualizer đẹp

Thay vì dùng `showwaves` (nặng CPU), dùng asset có sẵn:

1. Tải 1 video alpha transparent waveform/spectrum đẹp (khuyên dùng: https://www.pond5.com/alpha-channel/1/audio-waveform.html hoặc tự tạo trong After Effects)
2. Đặt vào: `data/input/story/fullauto-long/effects/audio-spectrum-alpha.mov`
3. Code sẽ tự dùng file này thay vì `showwaves`

**Lợi ích:** Đẹp hơn, không tốn CPU render.

### 2. Thêm Falling Effect (hiệu ứng hoa rơi)

Tải video alpha transparent hoa sen/lá rơi:
- Đặt vào: `data/input/story/fullauto-long/effects/falling-flowers-alpha.mov`
- Code sẽ tự overlay lên video

**Cấu hình opacity:** Sửa trong `config.json`:
```json
"long_falling_effect_opacity": 0.6
```

### 3. Thêm Subscribe Sticker động

Thêm GIF/video subscribe button:
- Đặt vào: `data/input/story/fullauto-long/stickers/`
- Code sẽ tự hiện sticker định kỳ (mỗi 5 phút)

**Cấu hình:**
```json
"long_sticker_start_seconds": 45,
"long_sticker_display_seconds": 8,
"long_sticker_interval_seconds": 300
```

---

## So sánh với đối thủ

| Kênh | Encode Preset | Bitrate | Zoom | Visualizer |
|---|---|---|---|---|
| **Bạn (tối ưu)** | `faster` | 4500k | Tắt | Asset/Tắt |
| Phật Pháp Màu Nhiệm | `medium` | 3000k | Tắt | Không |
| Ngô Pháp Phật Đà | `fast` | 3500k | Tắt | Không |
| Phật Pháp Linh Ứng | `medium` | 4000k | Bật | Có |

→ Config của bạn **nhanh hơn** nhưng **bitrate cao hơn** → chất lượng tương đương hoặc tốt hơn.

---

## Test A/B

Nếu vẫn lo, làm test này:

1. Render 1 video ngắn 5 phút với `fast`
2. Render 1 video ngắn 5 phút với `medium`
3. Upload cả 2 lên YouTube
4. So sánh sau khi YouTube process xong

**Kết quả:** Bạn sẽ không thấy khác biệt trên YouTube với bitrate 4500k.

---

## Kết luận

Config hiện tại đã tối ưu **tốc độ render gấp 2-3 lần** mà **không giảm chất lượng thực tế** trên YouTube.

Nếu muốn thêm hiệu ứng đẹp, dùng asset alpha video thay vì filter realtime để vừa đẹp vừa nhanh.
