# NX100 Web Control · Yaskawa UP50N

Ứng dụng web local: HTML/CSS/Vanilla JS → FastAPI trên PC → thư viện Ethernet Server hiện có → NX100 TCP port 80. UI đọc vị trí realtime. **Nhập tọa độ bằng bàn phím: Enter mới gửi MOVE. Slider và +/− tự gửi sau debounce.** Không có nút READ CURRENT POSITION hoặc MOVE.

## Chạy

Python 3.10 trở lên. Giữ project bên cạnh thư mục `nx100-remote-control-master` trong workspace: `robot/protocol.py` import thư viện đã kiểm chứng, không sửa protocol hoặc chương trình Tkinter cũ.

```powershell
cd 'C:\Data\TCP - YASKAWA\nx100_web_control'
python -m pip install -r requirements.txt
python app.py
```

Mở **http://127.0.0.1:8000** hoặc **http://localhost:8000**.

Workspace đã cài dependencies trong venv. Có thể chạy `..\.venv\Scripts\python.exe app.py`, hoặc mở `../Chay_NX100_Web.cmd`.

Backend chỉ bind loopback, một worker, không tự CONNECT. IP mặc định **192.168.255.1**, port **80**; sửa được trước CONNECT. Không chạy đồng thời app điều khiển cũ với robot thật: lock web chỉ có hiệu lực trong process này. Sau khi cài dependencies, giao diện và backend chạy offline, không cần CDN.

## Đọc realtime và tự gửi lệnh

1. Test khi vùng robot trống, dùng speed thấp; E-STOP phần cứng phải hoạt động.
2. CONNECT chỉ đọc trạng thái/Cartesian/Joint. Backend tiếp tục đọc `RSTATS`, `RPOSC 0,1`, `RPOSJ` mỗi 1000 ms, kể cả khi robot đứng yên. Browser lấy snapshot backend; nhiều tab không làm tăng tốc độ polling robot.
3. Input và slider tự điền bằng vị trí đọc được. Nhãn NOW và bảng CURRENT cập nhật realtime; input đang chỉnh và đích đang chạy được giữ riêng để polling không ghi đè thao tác.
4. **Nhập số bằng bàn phím: nhấn Enter mới gửi lệnh.** Dừng gõ bao lâu, Tab, click ra ngoài hoặc sự kiện change đều không gửi. Input và slider vẫn đồng bộ khi gõ; giá trị gõ dở được giữ riêng với đích đã xác nhận. Nếu đang gõ nhiều ô, Enter xác nhận từng ô; chỉ gửi khi không còn ô chưa xác nhận. UI hiển thị PRESS ENTER TO CONFIRM cho ô đang chờ.
5. **Kéo slider hoặc nhấn +/−** vẫn tự gửi sau 400 ms ngừng thay đổi, gom thành một đích cuối, không gửi từng pixel. Không dùng thao tác ở trục khác để gửi kèm số đang gõ dở chưa Enter. Slider thay thế bản nháp trên cùng trục; +/− tính từ giá trị đã xác nhận. Khi đã gửi lệnh, các ô chỉnh bị khóa đến khi backend xác nhận kết quả; **không xếp hàng đích tiếp theo** trong lúc robot đang chạy.
6. Backend đọc pose đầy đủ mới ngay trước mỗi MOVE để giữ đúng tool, posture và trục ngoài; sau đó đọc RSTATS mới. Chặn lệnh nếu mất kết nối, chưa Command Remote, Servo OFF, Running/HOLD/Alarm/Error ON, hoặc đang theo dõi lệnh trước. Không tự bật servo, nhả HOLD hay reset alarm.
7. CONNECT, polling, đổi tab, đổi MOVL/MOVJ, đổi speed, RESET UI và reload trang **không tạo chuyển động**. Speed chỉ áp dụng cho lần thay đổi tọa độ tiếp theo.
8. HOLD, DISCONNECT, RESET UI, đổi tab/motion/speed, rời cửa sổ hoặc ẩn trang hủy đích còn chờ trong debounce. Các trạng thái cản trở và mất kết nối cũng hủy đích chờ. Lệnh đã gửi tới NX100 không thể thu hồi bằng việc hủy timer; dùng HOLD khi cần dừng.

MOVL nhận đích Cartesian tuyệt đối hệ BASE, mm/s, mặc định 5. MOVJ cũng nhận đích Cartesian hệ BASE nhưng chạy theo khớp, đơn vị %, mặc định 2; quỹ đạo TCP không nhất thiết thẳng. XYZ nhận tối đa 3 chữ số thập phân; UI góc dùng 2 chữ số.

`CMD REMOTE` là bit Command Remote, không chỉ vị trí khóa REMOTE.

## Theo dõi hoàn tất và HOLD

`0000` chỉ là COMMAND ACCEPTED. Backend theo dõi status/pose và hiển thị TARGET/CURRENT/Δ ERROR. MOVE COMPLETE chỉ khi Running OFF, không có trạng thái cản trở và hai mẫu liên tiếp trong tolerance XYZ 1 mm, góc 0.5°. Sai số góc tính theo chu kỳ 360°. Dừng ngoài tolerance → STOPPED OFF TARGET; quá 60 giây → CONFIRMATION TIMEOUT. Không tự thử lại lệnh.

HOLD gửi `HOLD,1`, không phải Emergency Stop. UI phản hồi ngay HOLD REQUESTED; backend ưu tiên yêu cầu HOLD trước MOVE mới nhưng phải chờ giao dịch TCP đang thực hiện kết thúc/timeout. Timeout mỗi giai đoạn là 3 giây mặc định; tổng chờ có thể dài hơn. E-STOP phần cứng luôn phải hoạt động. App không tự nhả HOLD; xử lý tại pendant theo quy trình vận hành của tủ.

DISCONNECT, đóng browser hoặc dừng server không tự dừng robot. Backend tiếp tục theo dõi nếu browser đóng nhưng server còn chạy. Mất mạng/timeout báo lỗi, chuyển DISCONNECTED; reconnect thủ công chỉ đọc trạng thái và vị trí. Không replay đích cũ, không tự reconnect. Nếu mất phản hồi sau gửi MOVE/HOLD → OUTCOME UNKNOWN: kiểm tra robot thực tế, không gửi lại theo phỏng đoán.

## Joint / Pulse

J1/S … J6/T đọc realtime từ `RPOSJ` theo pulse, không tự đổi sang độ. Các ô Joint chỉ đọc.

MOVE pulse chưa được hỗ trợ; API `/api/move/joint` trả 501 `JOINT_MOVE_UNSUPPORTED`. Module hiện tại nhận XYZ/TX/TY/TZ cho MOVJ, không có lệnh tới đích pulse đã xác minh. Không dùng pulse làm XYZ. Không có camera, vacuum, JOB sequence hay AI vision ở phase này.

## MOCK riêng

```powershell
python app.py --mock
```

Hoặc mở `../Chay_NX100_Web_Mock.cmd`. Có banner SIMULATION MODE và badge MOCK, không tạo socket TCP tới robot thật. Mock mô phỏng Cartesian trong 3 giây, status/pose/HOLD; Joint pulse là mẫu độc lập, không phải tính động học. Chỉ chọn real/mock lúc chạy backend. Test MOCK không chứng minh vận hành với robot thật.

## Cấu hình

Sửa `config.json` rồi khởi động lại:

- `robot_ip`, `robot_port`, `timeout_seconds`.
- `poll_interval_ms`: mặc định 1000, tối thiểu 500; các command chạy tuần tự bằng lock, tick bận được bỏ qua.
- `auto_send_delay_ms`: mặc định 400, cho phép 100–2000 ms. Gom thay đổi slider và +/−; nhập bàn phím luôn đợi Enter, không dùng timeout này để gửi số đang gõ dở. Đây không phải chu kỳ servo hay chế độ streaming của NX100.
- `cartesian`, `joint`: range, step, nudge và unit của từng slider. Đây là **range UI ban đầu**, không phải workspace UP50N/joint limit đã được xác minh. Khi đọc ngoài range, UI mở rộng để hiển thị đúng số, không clamp pose. Backend không coi các range này là giới hạn an toàn.
- `speed.movl`, `speed.movj`: min/max/default/step/unit; backend kiểm tra speed theo config và protocol.
- `tolerance.xyz_mm`, `tolerance.angle_deg`, `move_timeout_seconds`: chỉ để xác nhận kết quả, không thay thế kiểm tra an toàn thực tế.

Backend không tính IK hoặc collision. Giới hạn và interlock của controller vẫn phải hoạt động.

## API

GET `/api/config`, `/api/status`, `/api/status?refresh=true`, `/api/position/cartesian`, `/api/position/joint`, `/api/logs?after=0`.

POST `/api/connect`, `/api/disconnect`, `/api/move/cartesian`, `/api/move/joint`, `/api/hold`.

POST dùng JSON, header `X-NX100-Control: local`. Host localhost/127.0.0.1, Origin nếu có phải cùng origin; không bật CORS. Browser chỉ gọi backend HTTP. Schema: `/openapi.json`.

Connect: `{"ip":"192.168.255.1","port":80}`.

Cartesian auto control không cần `reference_id`: backend lấy pose mới trong lock trước mỗi lệnh.

```json
{
  "motion": "MOVL", "speed": 5,
  "x": 1000, "y": 200, "z": 500,
  "rx": 180, "ry": 0, "rz": 90
}
```

API cũ có `reference_id` vẫn được hỗ trợ: nếu truyền ID từ GET position/cartesian thì kiểm tra pose chưa đổi trước MOVE. UI mới không dùng chế độ này.

MOVE được ưu tiên trước polling: chờ giao dịch đọc hiện tại nhả lock, tối đa `timeout_seconds` (mặc định 3 giây), rồi đọc pose/status mới và gửi đúng một lần. Poll bỏ qua các lần đọc tiếp theo khi MOVE đang chờ. Chỉ nhận một yêu cầu MOVE tại một thời điểm; yêu cầu MOVE thứ hai bị từ chối, không xếp hàng sau chuyển động trước. HOLD hoặc DISCONNECT hủy MOVE còn chờ lock; hết thời gian chờ cũng bỏ lệnh, không gửi muộn hay tự retry. READ thủ công đang bận vẫn có thể trả 409 BUSY. Log INFO/TX/RX/ERROR lưu vòng 600 sự kiện trong RAM; raw ẩn mặc định, bật SHOW RAW TX/RX để xem.

## Kiểm tra offline

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m compileall -q app.py robot tests
```

Khởi động server `--mock`, sau đó `python tests/browser_smoke.py` (Playwright + Microsoft Edge). Script từ chối test chuyển động nếu backend không phải MOCK.

Test controller/API bao gồm live poll không tạo motion, auto move không cần READ, lấy metadata mới, chặn Running/HOLD, lock, timeout, mất phản hồi và reconnect không replay. Browser test kiểm tra input/slider/nudge tự gửi sau debounce, gom đích, realtime không gửi lệnh, HOLD hủy timer, RESET/reconnect/reload không replay, Joint chỉ đọc, desktop/mobile và lỗi JavaScript. Ảnh mới: `qa/desktop.png`, `qa/mobile.png`. Chưa xác minh app web trên NX100 thật.

Tham chiếu: `../datasheet/33175396.pdf` (manual HW0483202) và thư viện NX100 hiện có.
