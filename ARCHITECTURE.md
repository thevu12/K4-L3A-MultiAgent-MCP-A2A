# L3A Architecture Record

Tài liệu này mô tả đúng implementation hiện tại của bài Day09 L3A. Repo không phải sản phẩm
MoveInMate; contract đang chấm bài toán điều tra khiếu nại thương mại điện tử. Nguồn chuẩn là
`contracts/` và không được thay bằng giả định từ nội dung khiếu nại.

## 1. Mục tiêu và ranh giới

Hệ thống nhận một case, truy xuất evidence có thẩm quyền qua MCP, phân công theo domain, kiểm tra
tính nhất quán và tạo hai artifact:

- `outputs/<case_id>.json` theo `day09-l3a-output-v2`;
- `traces/trace.jsonl` theo `day09-trace-event-v1`.

Hệ thống không tự tạo `evidence_ref`, không dùng evidence chéo case và không tự gửi hoàn tiền.
`financial_resolution` chỉ là khuyến nghị; implementation hiện để số tiền bằng 0 khi chưa có rule
đủ căn cứ để tính chính xác.

```text
Input case
    |
    v
Coordinator -- tool discovery --> MCP inventory
    |
    +--> Order/Item Agent ---- get_order, get_order_items, get_sellers
    +--> Payment Agent ------- get_order_payments, get_payment_timeline,
    |                           get_refund_timeline
    +--> Shipment Agent ------ get_shipment_summary
    +--> Policy Agent -------- get_policy
    |                                  |
    +---------- EvidenceResult --------+
                       |
                       v
                    Verifier
                       |
                       v
                 Output + Trace
```

Luồng chạy thật: `student_agent.cli:_run()` → `workflow.solve_case()` →
`EvidenceGateway.call()` → verifier/rule engine → contract validation → ghi output.

## 2. Thành phần và ownership

| Actor | Đầu vào | Trách nhiệm | Đầu ra |
| --- | --- | --- | --- |
| Coordinator | case + inventory đã discovery | xác thực `case_id`, tạo `ToolTask`, phát `task_assigned`, finalize | task và output cuối |
| Order/Item Agent | `claimed_order_id` | lấy order, item, seller; không kết luận payment | evidence các entity |
| Payment Agent | `claimed_order_id` | lấy payment, payment lifecycle và refund lifecycle | evidence tài chính |
| Shipment Agent | `claimed_order_id` | lấy shipment summary và dấu hiệu chậm giao | evidence giao vận |
| Policy Agent | `policy_version` | lấy policy đúng phiên bản | policy evidence |
| Verifier | tất cả `EvidenceResult` | áp rule bảo thủ, chọn evidence liên quan, tạo decision | output fields + `verification_completed` |

`ToolTask` và `EvidenceResult` là message nội bộ có kiểu rõ ràng. Specialist chỉ được gọi tên tool
có trong inventory MCP. Candidate không tồn tại sẽ được trace là `TOOL_UNAVAILABLE` và không có
network call.

## 3. Phối hợp A2A quan sát được

Không có message broker ngoài. A2A được thể hiện bằng state transition và trace công khai:

```text
case_received
  -> task_assigned(coordinator -> specialist)
  -> tool_result_consumed(specialist, chỉ khi call thành công)
  -> handoff(specialist -> verifier)
  -> verification_completed(verifier)
  -> case_finalized(coordinator)
```

Mỗi task kết thúc đúng một lần bằng `handoff`. `attributes` chỉ chứa scalar theo trace schema;
không ghi prompt, chain-of-thought, API key hoặc raw customer data. Workflow chạy tuần tự theo case
để trace ổn định và tránh trộn evidence.

## 4. Tool discovery và evidence lifecycle

Inventory thực tế đã được xác minh từ MCP ngày 2026-09-25 gồm:

- core L3A: `get_order`, `get_order_items`, `get_order_payments`, `get_shipment_summary`,
  `get_sellers`, `get_policy`;
- lifecycle/context: `get_payment_timeline`, `get_refund_timeline`, `get_customer_history`,
  `get_product_context`.

Implementation chỉ gọi tám tool cần cho output L3A; customer history và product context chưa được
gọi vì input hiện không cung cấp identity/product key cần thiết và scorer L3A không yêu cầu các
field đó. Tham số đã đối chiếu với schema MCP: các tool theo đơn nhận `case_id + order_id`, còn
`get_policy` nhận `case_id + policy_version`.

Evidence lifecycle:

1. CLI discovery tool một lần cho cả run.
2. Coordinator chọn tool từ inventory, không đoán tool ngoài inventory.
3. Gateway gọi tool kèm đúng `case_id` và validate envelope MCP.
4. Specialist chỉ lưu `data` và `evidence_ref` đã validate.
5. Evidence dùng để kết luận được đưa vào output và liên kết bằng `tool_result_consumed`.
6. Danh sách ID/evidence được deduplicate và giới hạn theo public schema; evidence chỉ dùng để
   điền entity không tự động được thêm vào tập evidence của quyết định.

## 5. Decision policy

Verifier ưu tiên quyết định có dấu hiệu tường minh trong evidence:

1. `refund_failed` hoặc `refund_pending` từ payment/refund lifecycle;
2. `duplicate_charge` chỉ khi có flag/anomaly rõ ràng, không suy ra từ việc có hai payment;
3. order đã hủy/không có hàng nhưng còn captured amount chưa refund;
4. nhiều payment có tổng bằng order total là `valid_split_payment`, lệch tổng là
   `payment_mismatch`;
5. giao trễ chỉ phân trách nhiệm seller/logistics khi shipment evidence nêu nguồn chậm;
6. `unsupported_claim` chỉ khi policy evidence thể hiện không hỗ trợ;
7. còn lại là `insufficient_evidence` và `needs_investigation`.

Mỗi claim giữ nguyên `claim_id` từ input. Khi input không có claim, hệ thống để danh sách rỗng,
không tạo `claim-unknown`. Confidence là hằng số hiệu chỉnh theo độ mạnh của từng rule; chưa phải
xác suất học từ dữ liệu.

## 6. Failure policy

| Failure | Retry | Hành vi | Trace |
| --- | ---: | --- | --- |
| Tool không có trong discovery | 0 | bỏ domain, hạ mức kết luận | `task_assigned=TOOL_UNAVAILABLE`, `handoff` |
| MCP/network/contract call lỗi | 0 | không dùng data/ref giả, tiếp tục domain khác | `handoff=INSUFFICIENT_EVIDENCE` + loại lỗi |
| Thiếu `claimed_order_id` | 0 | không gọi MCP | coordinator handoff thẳng verifier |
| Thiếu evidence order/payment | 0 | `insufficient_evidence` | `verification_completed` |
| Dữ liệu không khớp rule đã biết | 0 | `insufficient_evidence` | `verification_completed` |

Không retry tự động vì MCP call được audit và chưa có idempotency/retry contract công khai. Có thể
bổ sung retry sau khi server xác nhận error taxonomy và idempotency.

## 7. Model dưới 10 tỷ tham số

### Runtime hiện tại

Runtime **không dùng mô hình sinh**. Coordinator, specialist routing và verifier là Python
deterministic. Lựa chọn này tránh hallucination evidence, không thêm dependency inference và phù
hợp với output contract cần tái lập. Vì không có model active nên hệ thống không vi phạm giới hạn
dưới 10 tỷ tham số.

### Phương án mở rộng đã duyệt

Nếu bài học yêu cầu có LLM, model duy nhất được allowlist là `Qwen/Qwen3-8B` (8.2B parameters).
Model card chính thức mô tả khả năng multilingual và tool/agent, phù hợp để route claim hoặc giải
thích quyết định: <https://huggingface.co/Qwen/Qwen3-8B>.

LLM chỉ được đặt trước verifier hoặc sau output để giải thích; không được tạo/sửa `evidence_ref`,
tự tính tiền hoàn, bỏ qua JSON Schema hay thay thế deterministic safety rules. Việc tích hợp này
chưa được triển khai và không được tuyên bố là đã chạy.

## 8. Verification invariants

Trước khi ghi file, CLI kiểm tra:

- output pass `l3a-output-v2.schema.json` và `case_id` khớp input;
- mọi entity/evidence list unique và không vượt giới hạn;
- evidence chỉ đến từ MCP response đã validate;
- evidence trong output đã xuất hiện ở `tool_result_consumed`, không bị dùng chéo case;
- failure/partial evidence không bị đổi thành `unsupported_claim`;
- trace pass schema, đủ lifecycle theo đúng thứ tự và có actor chuyên biệt;
- secret không xuất hiện trong output/trace/submission.

Các test giả lập bao phủ explicit duplicate, valid split payment, policy-denied claim, MCP failure,
tool discovery, schema output, trace collaboration/finalization, deduplicate entity và không tạo
claim giả.

## 9. Reproducibility và giới hạn đã biết

- Python: 3.11+; dependency pin theo `pyproject.toml`.
- Cài đặt: `python3.11 -m venv .venv && .venv/bin/pip install -e '.[dev]'`.
- Kiểm tra: `.venv/bin/ruff check .` và `.venv/bin/pytest -q`.
- Chạy thật: `.venv/bin/day09 validate-inputs`, `mcp-tools`, `run`, `validate`, `package`.
- Concurrency: tuần tự; không dùng random seed cho decision.
- Không ghi model/API key vào `.env.example`, trace hoặc output.

Repo local đã có đủ 100 input và `day09 validate-inputs` kiểm tra inventory trước khi chạy. Semantic
end-to-end và submission vẫn phụ thuộc Team API Key hợp lệ cùng MCP Gateway đang cấp evidence.
Shape `data` của MCP không nằm trong public contract; parser chỉ hỗ trợ các layout đã mô tả bằng
order/payment/refund/shipment/policy record và cố ý fallback an toàn khi không nhận diện được.
