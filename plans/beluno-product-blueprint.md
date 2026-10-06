# Beluno — Product Blueprint

> Một workspace mobile-first cho mọi kế hoạch của nhóm bạn: cùng quyết định, tổ chức, chia tiền và lưu lại kỷ niệm trong một nơi.

**Tên sản phẩm:** Beluno  
**Tagline:** Plans with your people.  
**Tài liệu:** Product strategy + product requirements + technical blueprint  
**Phiên bản:** 1.0  
**Ngày:** 06/10/2026  
**Trạng thái:** Unified product scope / brand selected  
**Đối tượng đọc:** Founder, product designer, mobile developer, backend developer, growth/marketing

---

## Mục lục

1. [Executive summary](#1-executive-summary)
2. [Problem](#2-problem)
3. [Target users, personas và jobs-to-be-done](#3-target-users-personas-và-jobs-to-be-done)
4. [Positioning](#4-positioning)
5. [Competitive framing](#5-competitive-framing)
6. [Core product principles](#6-core-product-principles)
7. [End-to-end user journey](#7-end-to-end-user-journey)
8. [Information architecture](#8-information-architecture)
9. [Feature modules](#9-feature-modules)
10. [Unified product scope](#10-unified-product-scope)
11. [Unified delivery model và workstreams](#11-unified-delivery-model-và-workstreams)
12. [Monetization](#12-monetization)
13. [Growth loops](#13-growth-loops)
14. [Retention strategy](#14-retention-strategy)
15. [Onboarding](#15-onboarding)
16. [Notifications](#16-notifications)
17. [Edge cases và business rules](#17-edge-cases-và-business-rules)
18. [Security và privacy](#18-security-và-privacy)
19. [Analytics và KPIs](#19-analytics-và-kpis)
20. [Technical architecture](#20-technical-architecture)
21. [Suggested stack](#21-suggested-stack)
22. [Database schema](#22-database-schema)
23. [API considerations](#23-api-considerations)
24. [Offline-first và sync strategy](#24-offline-first-và-sync-strategy)
25. [Test strategy](#25-test-strategy)
26. [Launch plan](#26-launch-plan)
27. [ASO và content ideas](#27-aso-và-content-ideas)
28. [Risks và mitigations](#28-risks-và-mitigations)
29. [Product boundaries](#29-product-boundaries)
30. [Kế hoạch thực thi single-scope](#30-kế-hoạch-thực-thi-single-scope)
31. [Definition of success và quyết định còn mở](#31-definition-of-success-và-quyết-định-còn-mở)
32. [Nguồn tham khảo](#32-nguồn-tham-khảo)

---

## 1. Executive summary

### 1.1 Ý tưởng cốt lõi

Beluno lấy phần lõi đã được chứng minh của Splitwise — **ghi chi tiêu, chia tiền, tính số dư và settle** — rồi kết nối nó với toàn bộ workflow lập kế hoạch nhóm. Một `Plan` có thể là dinner, coffee, movie, sport, birthday, weekend outing hoặc multi-day trip.

Một kế hoạch nhóm hiện thường bị phân mảnh giữa nhiều công cụ:

- Messenger/WhatsApp để trao đổi;
- Notes/Google Docs để ghi lịch;
- Google Maps để lưu địa điểm;
- Sheets để lập ngân sách;
- Splitwise để chia tiền;
- email/Drive để giữ booking hoặc vé;
- album ảnh để lưu kỷ niệm.

Beluno gom các workflow quan trọng vào một workspace duy nhất. Sản phẩm chủ động không thay thế chat, bản đồ dẫn đường hay dịch vụ booking; đây là ranh giới sản phẩm lâu dài, không phải sự trì hoãn feature. Beluno là lớp điều phối chung giúp cả nhóm trả lời nhanh năm câu hỏi:

1. Nhóm sẽ làm gì, lúc nào và ở đâu?
2. Ai phụ trách việc gì?
3. Đã chi bao nhiêu và còn bao nhiêu ngân sách?
4. Ai nợ ai?
5. Việc hoặc quyết định nào chưa chốt?

### 1.2 Product thesis

Người dùng không thực sự cần thêm một event app, itinerary app hoặc expense app riêng lẻ. Họ cần một nơi khiến cả nhóm **biến “hôm nào đi” thành một kế hoạch thực sự diễn ra**, dù đó là bữa tối hai tiếng hay chuyến đi hai tuần.

Thesis của sản phẩm:

> Nếu `Plan` là nguồn ngữ cảnh chung và expense ledger là nguồn sự thật tài chính đáng tin cậy, Beluno trở thành hệ điều hành nhẹ cho mọi kế hoạch của nhóm bạn.

### 1.3 Wedge vào thị trường

Không launch với thông điệp “all-in-one super app”. Thông điệp wedge phải cụ thể:

> **Turn group-chat ideas into plans that actually happen.**

Trong cùng một product scope, hành trình khám phá giá trị đi theo thứ tự:

```text
Create a plan
→ Invite your people
→ Decide together
→ Coordinate and spend
→ Settle and remember
```

### 1.4 Đối tượng trọng tâm

Nhóm bạn 3–12 người, 18–35 tuổi, thường xuyên tổ chức ăn uống, vui chơi, thể thao, sinh nhật hoặc du lịch. Đây là nhóm có pain lặp lại, invite loop tự nhiên và đủ nhiều tình huống để tạo retention quanh cùng một crew.

### 1.5 North-star metric

**Weekly Active Plans with Meaningful Collaboration (WAP-MC)**

Một plan được tính là “meaningful collaboration” trong tuần nếu có:

- ít nhất 2 thành viên hoạt động; và
- ít nhất 3 hành động có giá trị, trong đó tối thiểu 1 hành động tài chính;
- ví dụ: thêm expense, sửa split, ghi settlement, vote, thêm itinerary item hoặc hoàn tất responsibility.

Metric này tốt hơn MAU vì đơn vị giá trị thật là **plan có nhiều người cùng tham gia**, không phải tài khoản mở app một mình.

### 1.6 Phạm vi thống nhất

Beluno được xác định là **một product scope duy nhất**, gồm đầy đủ:

- groups, plans, participants, RSVP và quyền truy cập;
- expenses, split rules, balances, settlement và multi-currency;
- budgets, schedule, places, polls và bookings;
- checklists, packing, responsibilities và virtual plan fund;
- offline-first sync, activity/audit, search và export;
- memories, recap và plan reuse;
- onboarding, notifications, monetization, analytics, privacy và support tooling cần thiết.

Các workstream có thể được triển khai theo thứ tự phụ thuộc kỹ thuật, nhưng không được dùng thứ tự đó để chia sản phẩm thành các scope hay release feature riêng. Definition of done là toàn bộ phạm vi trên tích hợp, kiểm thử và sẵn sàng vận hành cùng nhau.

---

## 2. Problem

### 2.1 Vấn đề chức năng

Trong mọi kế hoạch nhóm, dữ liệu bị chia nhỏ và nhanh chóng mất ngữ cảnh:

- expense nằm trong app tài chính nhưng không biết liên quan booking/activity nào;
- booking hoặc reservation nằm trong email của một người;
- thời gian/lịch trình nằm trong ảnh chụp hoặc tin nhắn ghim;
- địa điểm nằm rải rác trong link Maps;
- ngân sách dự kiến nằm trong Sheets nhưng chi tiêu thực tế ở nơi khác;
- danh sách ai phụ trách việc gì không có owner rõ;
- sau chuyến đi, nhóm phải tính lại bằng tay hoặc tranh luận vì dữ liệu không đầy đủ.

### 2.2 Vấn đề cảm xúc và xã hội

Pain không chỉ là toán học. Tiền bạc và trách nhiệm dễ tạo căng thẳng:

- ngại nhắc bạn trả tiền;
- không nhớ ai đã trả booking nào;
- một người phải đóng vai “trip manager” cho cả nhóm;
- người lên kế hoạch cảm thấy gánh nặng không được chia đều;
- quyết định chậm vì group chat quá nhiều tin;
- sai số nhỏ về tỷ giá hoặc rounding tạo cảm giác thiếu công bằng.

### 2.3 Vấn đề theo từng giai đoạn

| Giai đoạn | Pain chính | Hậu quả |
|---|---|---|
| Ý tưởng | Chốt người, ngày, địa điểm và lựa chọn | Kế hoạch chết trong group chat |
| Chuẩn bị | RSVP, booking, task, checklist và budget | Một người ôm việc, thông tin thất lạc |
| Diễn ra | Theo lịch, cập nhật thay đổi và ghi expense | Người đến sai chỗ/giờ, quên chi tiêu |
| Kết thúc | Settle, recap, lưu ảnh và reuse plan | Tranh luận tiền bạc, ký ức phân mảnh |

### 2.4 Root causes

1. Các công cụ hiện tại tối ưu cho chat, calendar hoặc expense riêng lẻ, không tối ưu cho **group-plan lifecycle**.
2. Nhóm thiếu một nguồn sự thật chung có audit trail.
3. Workflow nhập liệu quá nặng nên dữ liệu nhanh chóng lỗi thời.
4. App event thường dừng ở invite/RSVP; app chia tiền thiếu ngữ cảnh; app travel quá nặng cho các cuộc hẹn thường ngày.
5. Một data model bị khóa vào “event” hoặc “trip” không theo được toàn bộ phổ kế hoạch của cùng một nhóm bạn.

### 2.5 Problem statement

> Các nhóm bạn cần một workspace chung để biến ý tưởng thành kế hoạch thực sự diễn ra, giữ người, quyết định, trách nhiệm, tiền bạc và kỷ niệm luôn đồng bộ mà không phải ghép nhiều app.

---

## 3. Target users, personas và jobs-to-be-done

### 3.1 Beachhead segment

**Nhóm bạn 3–12 người thường xuyên tổ chức các kế hoạch cùng nhau.**

Đặc điểm:

- smartphone-first;
- quen dùng invite link và app consumer;
- có ít nhất 2 người thay phiên thanh toán;
- có kế hoạch từ casual meetup đến multi-day trip;
- không muốn duy trì spreadsheet phức tạp;
- thường có RSVP, quyết định nhóm, task hoặc khoản chi cần settle.

### 3.2 Secondary segments

- cặp đôi và nhóm bạn thân;
- gia đình nhiều thế hệ;
- nhóm ăn uống/cà phê/xem phim;
- nhóm thể thao hoặc hobby định kỳ;
- nhóm road trip;
- nhóm sinh viên backpacking;
- retreat/offsite nhỏ của công ty;
- nhóm thường xuyên tổ chức trip theo cùng một crew.

Ngoài target segment: tour operator, travel agency, enterprise expense management và solo traveler.

### 3.3 Personas

#### Persona A — Linh, Group Organizer

- 28 tuổi, thường là người mở group chat và chốt lịch.
- Pain: phải nhắc mọi người, giữ booking, lập Sheets và trả trước.
- Goal: nhìn một màn hình là biết plan đã chốt chưa và còn việc gì.
- Activation moment: mời bạn bè, có RSVP và chốt được lựa chọn đầu tiên.
- Willingness to pay: cao nhất nếu app giảm gánh nặng điều phối.

#### Persona B — Minh, Frequent Payer

- 30 tuổi, có thẻ tín dụng và thường đặt hotel/flight cho cả nhóm.
- Pain: ứng tiền lớn, khó theo dõi ai đã hoàn trả.
- Goal: ledger minh bạch, tỷ giá rõ, settlement ít giao dịch.
- Activation moment: thấy balance tính đúng cho một expense phức tạp.

#### Persona C — An, Passive Participant

- 24 tuổi, ít muốn cài app hoặc setup tài khoản.
- Pain: không muốn điền nhiều thông tin; thường chỉ cần xem lịch và trả tiền.
- Goal: join trong dưới 30 giây, biết mình nợ bao nhiêu và việc của mình là gì.
- Activation moment: mở invite link, vào trip ngay, không phải tạo workspace từ đầu.

#### Persona D — Quân, Detail-oriented Planner

- 32 tuổi, đặt budget và muốn kiểm soát overspend.
- Pain: chi tiêu thực tế không khớp spreadsheet.
- Goal: ngân sách cập nhật tự động theo expense và theo ngày.
- Activation moment: nhìn thấy remaining daily budget thay đổi ngay sau khi thêm expense.

### 3.4 Jobs-to-be-done

#### Functional jobs

- Khi một người thanh toán, tôi muốn ghi nhanh ai tham gia để không phải nhớ sau.
- Khi cả nhóm dùng nhiều loại tiền, tôi muốn biết số dư theo base currency nhưng vẫn giữ số tiền gốc.
- Khi chuẩn bị trip, tôi muốn biết ai đã book/trả khoản nào.
- Khi lịch thay đổi, tôi muốn mọi người thấy cùng một thông tin.
- Khi trip kết thúc, tôi muốn settle bằng ít giao dịch nhất.

#### Emotional jobs

- Tôi muốn cảm thấy tiền bạc minh bạch và công bằng.
- Tôi muốn tránh trở thành người duy nhất phải quản lý mọi thứ.
- Tôi muốn tự tin rằng app vẫn hoạt động khi mạng yếu.
- Tôi muốn nhớ chuyến đi như một trải nghiệm, không phải một bảng nợ.

#### Social jobs

- Tôi muốn nhắc người khác hoàn tất việc mà không tạo cảm giác khó chịu.
- Tôi muốn chia sẻ một recap đẹp cho cả nhóm.
- Tôi muốn được ghi nhận cho các khoản đã trả và công việc đã làm.

---

## 4. Positioning

### 4.1 Category

**Collaborative group-plans workspace** với decision, coordination và financial ledger là lõi.

Không tự định vị là:

- online travel agency;
- travel discovery engine;
- navigation/map app;
- social network;
- ngân hàng hoặc ví điện tử;
- enterprise expense platform.

### 4.2 Positioning statement

> Dành cho nhóm bạn thường xuyên lên kế hoạch cùng nhau, Beluno là workspace chung kết nối quyết định, lịch trình, trách nhiệm và chi tiêu trong một plan. Khác với app sự kiện hoặc chia tiền riêng lẻ, Beluno giữ toàn bộ hành trình từ ý tưởng đến quyết toán và kỷ niệm trong một nơi.

### 4.3 One-liner

> **Plan together. Spend together. Settle without stress.**

Phiên bản rõ nghĩa hơn:

> **One place for every plan with your people.**

### 4.4 Value proposition theo persona

| Persona | Giá trị chính |
|---|---|
| Organizer | Một dashboard cho việc cần làm, booking, budget và pending decisions |
| Frequent payer | Ledger, rate snapshot, balance và settle minh bạch |
| Passive member | Join nhanh, nhập ít, chỉ thấy thứ liên quan mình |
| Budgeter | Budget theo category, burn rate và remaining daily budget |

### 4.5 Moat khả thi

Moat không đến từ việc có nhiều feature. Nó có thể hình thành từ:

1. **Plan graph:** quan hệ giữa group, participant, decision, expense, place, booking, activity và memory.
2. **Collaboration history:** nhóm cũ tạo plan mới nhanh hơn với default people/splits/preferences.
3. **Trust in ledger:** tính đúng, audit được, sync ổn định.
4. **Workflow integration:** poll → schedule; booking → expense; schedule → actual cost; plan → recap.
5. **Invite network:** mỗi organizer kéo nhiều thành viên vào sản phẩm.

---

## 5. Competitive framing

### 5.1 Khung cạnh tranh

| Nhóm sản phẩm | Điểm mạnh | Khoảng trống Beluno có thể khai thác |
|---|---|---|
| Expense splitters như Splitwise | Ledger, split rules, balances, settlement | Không lấy plan lifecycle làm trung tâm; plan/budget/booking context hạn chế |
| Travel expense trackers như TravelSpend | Budget, foreign currency, offline, travel context | Coordination và planning chưa phải trọng tâm lớn nhất |
| Collaborative planners như Wanderlog | Itinerary, places, map, collaboration, reservations | Financial ledger không nhất thiết là nguồn sự thật cốt lõi |
| Itinerary organizers như TripIt | Reservation import, itinerary, flight/travel information | Group decision, shared responsibility và expense settlement không phải wedge |
| General tools: Sheets, Notes, chat | Linh hoạt, quen thuộc, gần như miễn phí | Dữ liệu rời rạc, manual, không có domain logic hoặc settlement |

### 5.2 Strategic wedge

Beluno không nên cố thắng tất cả đối thủ trên mọi feature. Trình tự cạnh tranh:

1. **Thắng Splitwise trong use case trip:** offline-first, multi-currency rõ, budget-native, booking-linked expenses.
2. **Thắng spreadsheet:** setup nhanh, tự tính, collaboration và audit trail.
3. **Tích hợp thay vì thay thế map/chat:** deep link tới Google/Apple Maps; share link vào chat.
4. **Mở rộng có chọn lọc sang planning:** itinerary tối giản, places, polls và responsibilities.

### 5.3 Differentiators cần chứng minh

- Thêm expense trong dưới 15 giây.
- Không mất dữ liệu khi offline hoặc sync conflict.
- Mọi conversion có thể giải thích: original amount, rate, rate source và thời điểm.
- Balance đúng và tái tạo được từ ledger.
- Từ booking/activity có thể tạo expense mà không nhập lại dữ liệu.
- Join bằng invite link với ma sát tối thiểu.

### 5.4 Clone responsibly

Clone **problem model và proven workflow**, không clone:

- tên, logo, icon, copywriting hoặc brand assets;
- screen layout pixel-for-pixel;
- proprietary illustrations hoặc onboarding copy;
- các chi tiết interaction mang tính nhận diện;
- dữ liệu hoặc API không được phép sử dụng.

Thiết kế phải có information architecture, visual language và interaction model riêng, xoay quanh plan lifecycle.

---

## 6. Core product principles

### 6.1 Financial truth first

Ledger là nguồn sự thật. Mọi balance và summary phải được tính từ transaction/split records; không chỉnh số dư trực tiếp.

### 6.2 Fast at the moment of payment

Use case quan trọng nhất xảy ra khi người dùng đang đứng ở quầy, mạng yếu và cả nhóm muốn đi tiếp. Flow add expense phải ngắn, nhớ default hợp lý và hoạt động offline.

### 6.3 Explain every number

User phải chạm vào một con số và hiểu nó đến từ đâu: khoản nào, ai trả, ai tham gia, tỷ giá nào, rounding ra sao.

### 6.4 Collaboration without forcing behavior

App hỗ trợ nhóm đang dùng Messenger/WhatsApp thay vì bắt họ chuyển chat vào app. Mọi object quan trọng đều share/deep-link được.

### 6.5 Progressive disclosure

Default đơn giản; split nâng cao, custom FX, itemization và exceptions chỉ xuất hiện khi cần.

### 6.6 Offline is a state, not an error

User vẫn tạo/sửa dữ liệu khi offline. UI phải nói rõ trạng thái pending, synced hoặc conflict mà không làm họ lo lắng.

### 6.7 One action, one owner

Booking, responsibility và poll outcome nên có owner hoặc trạng thái rõ. Tránh “mọi người cùng chịu trách nhiệm” nhưng thực tế không ai làm.

### 6.8 Reversible by default

Các thay đổi tài chính nhạy cảm cần version history, soft delete hoặc reversal. Không để edit im lặng làm thay đổi lịch sử mà thành viên không biết.

### 6.9 Privacy proportional to sensitivity

Plan data mặc định private. Không public schedule, booking code, location hoặc balance. Quyền truy cập dựa trên participation/membership.

### 6.10 Focus beats feature count

Mỗi module mới phải chứng minh ít nhất một trong ba điều:

- tăng activation;
- tăng số thành viên hợp tác;
- tăng khả năng một group tạo plan tiếp theo.

---

## 7. End-to-end user journey

### 7.1 Stage 0 — Discover

**Trigger:** một người muốn biến ý tưởng trong group chat thành plan có người, thời gian, địa điểm và trách nhiệm rõ ràng.

Flow:

```text
Search / recommendation / invite
→ Landing or store page
→ See promise: every group plan in one place
→ Install or continue on lightweight web join
```

Success signal: user hiểu trong 5 giây rằng đây là app cho **plans with friends**, từ dinner đến trip.

### 7.2 Stage 1 — Create plan

Organizer nhập tối thiểu:

- plan title;
- kind/template: dinner, coffee, movie, sport, birthday, outing, trip hoặc custom;
- date/time hoặc “decide later”;
- group/participants.

Các trường tùy chọn trong cùng flow:

- place hoặc destination;
- total budget;
- default expense split;
- timezone;
- cover image.

Output: plan workspace và invite link.

### 7.3 Stage 2 — Invite and join

```text
Organizer shares link
→ Member opens link
→ Sees plan title, time/place if available, organizer
→ Chooses display name
→ Joins as guest or signs in
→ Can claim/upgrade guest identity without leaving the plan
```

Các safeguard:

- invite có thể revoke/rotate;
- organizer duyệt join nếu plan bật approval;
- không lộ booking/balance trước khi membership được xác nhận.

### 7.4 Stage 3 — Decide and prepare

Nhóm:

- vote ngày, địa điểm hoặc hoạt động;
- RSVP going/maybe/declined;
- đặt budget khi cần;
- thêm prepaid expense/booking/reservation;
- gán responsibilities;
- xây schedule hoặc itinerary;
- tạo checklist/packing list;
- xác nhận ai tham gia từng activity.

Home tập trung vào “cần chốt gì tiếp theo”, không chỉ feed dữ liệu.

### 7.5 Stage 4 — During the plan

Primary loop:

```text
Pay
→ Add expense in <15 seconds
→ Auto-select recent payer/currency/members
→ Save locally
→ Group balances and budget update
→ Sync when network is available
```

Secondary loop:

```text
Open Today
→ See next activity/booking
→ Navigate via external map
→ Attach actual expense
→ Mark activity done
```

### 7.6 Stage 5 — Settle

Khi plan sắp kết thúc:

1. App nhắc review incomplete expenses và refunds.
2. Mọi thành viên confirm ledger hoặc flag khoản cần xem.
3. App freeze một settlement preview ở base currency hoặc per-currency.
4. Thuật toán đề xuất số giao dịch tối thiểu.
5. Thành viên trả tiền ngoài app và record payment.
6. Plan đạt trạng thái settled khi balance trong tolerance.

### 7.7 Stage 6 — Recap and reuse

Recap gồm:

- tổng chi và category breakdown;
- cost per person/day;
- top places/activities;
- plan timeline;
- ảnh do nhóm chọn;
- fun stats không gây xấu hổ;
- trạng thái settlement.

CTA cuối:

- create another plan with same group;
- duplicate selected schedule/checklist/default splits;
- export data;
- share privacy-safe recap.

---

## 8. Information architecture

### 8.1 App-level navigation

```text
Home / Plans
├── Upcoming
├── Active
├── Past
└── Create plan

Notifications
Profile & settings
```

### 8.2 Plan-level navigation

Đề xuất 4 tab để tránh quá tải dù product scope đầy đủ:

```text
Overview
Expenses
Plan
More
```

- **Overview:** today, budget, balance, upcoming, pending items.
- **Expenses:** transaction feed, quick add, balances, settle.
- **Plan:** itinerary, places, polls và bookings.
- **More:** members, settings, budget, polls, places, packing, responsibilities, files, recap.

Không nên có 10 tab ngang hàng.

### 8.3 Overview hierarchy

Theo plan phase:

| Plan phase | Ưu tiên trên Overview |
|---|---|
| Planning | invite progress, pending decision, booking deadline, projected budget |
| Active | today, quick expense, spent today, next booking, offline state |
| Ending | missing expenses, unsettled balances, review ledger |
| Past | settlement status, recap, export, reuse crew |

---

## 9. Feature modules

### 9.1 Plan

**Purpose:** container cho mọi dữ liệu và quyền truy cập.

**Core fields:** title, kind, timing mode, dates/timezone, optional place/destination, base currency, status, cover, owner, budget, invite settings.

**Statuses:** `draft`, `planning`, `active`, `settling`, `completed`, `archived`.

**Key actions:** create, edit, duplicate, archive, leave, transfer ownership, export, delete.

**Rules:**

- start/end date có thể đổi; analytics và daily budget phải recompute;
- base currency đổi không được mutate historical original amounts;
- `plan_kind` chỉ cung cấp defaults/presentation, không chia product thành các model riêng;
- delete plan cần grace period và owner confirmation;
- transfer ownership bắt buộc trước khi owner rời plan nếu còn participant;
- trip-specific destination/segments nằm trong travel extension tùy chọn.

### 9.2 Members

**Roles:**

- `owner`: quản lý plan, quyền, billing;
- `admin`: quản lý members và hầu hết content;
- `member`: tạo/sửa content theo policy;
- `viewer`: read-only;
- `guest`: identity tạm, có thể claim sau.

**Member states:** invited, active, left, removed, pending approval.

**Core capabilities:** invite link, invite contact, revoke link, remove member, transfer ownership, per-member default share.

**Important rule:** Không xóa member khỏi lịch sử financial nếu họ đã có transaction. Chuyển sang inactive/removed và giữ reference.

### 9.3 Expenses

**Expense fields:** description, amount minor units, currency, paid date/time, payer(s), participants, category, merchant/place, notes, receipt, linked booking/activity, created by, sync/audit metadata.

**Quick-add default:**

1. amount;
2. description/category;
3. paid by current user;
4. split equally among active members;
5. currency = last used or inferred from trip location;
6. save.

**Advanced:** multiple payers, itemization, tax/tip, recurring/prepaid, refund, reimburse-only, personal/non-shared expense.

**Ledger rule:** Use integer minor units, never floating-point money. JPY has zero decimal; currencies with 3 decimals must be supported by currency metadata.

### 9.4 Split rules

Supported split rules:

- equal;
- exact amounts;
- percentage;
- shares/weights;
- exclude member.

- itemized bill;
- default split template;
- group/subgroup split;
- adults vs children ratio;
- nights stayed or days joined.

Validation:

- exact amounts sum to expense total;
- percentages sum to 100%;
- shares are positive integers/decimals according to policy;
- deterministic rounding assigns remainder by a documented rule;
- no participant with negative owed amount in standard expense;
- refund uses a separate transaction type, not negative expense hidden in UI.

### 9.5 Balances

Balance is derived, not editable.

For each expense:

```text
net(member) = amount_paid_by_member - amount_owed_by_member
```

For a currency or chosen settlement basis:

```text
plan_net(member) = Σ expense_nets + Σ settlement_adjustments
```

Views:

- “You owe / You are owed”;
- all member balances;
- per currency;
- base-currency estimate;
- explainable breakdown by expense.

Invariant: tổng net của tất cả member trong cùng một settlement basis phải bằng 0 trong tolerance rounding.

### 9.6 Settlement

**Modes:**

- settle in each original currency;
- convert all eligible balances to base currency using frozen rates;
- manual custom settlement amount;
- record cash/bank transfer/external payment.

**Debt simplification:**

1. tạo danh sách debtors và creditors từ net balance;
2. match largest debtor với largest creditor;
3. tạo transfer min(abs(debt), credit);
4. lặp cho tới khi mọi balance trong tolerance.

Thuật toán greedy thường cho kết quả tốt và dễ giải thích; không tuyên bố “minimum mathematically guaranteed” nếu chưa dùng optimization chính xác.

**Safety:** settlement không xóa expenses. Nó thêm payment transaction. Reversal tạo entry ngược thay vì mutate lịch sử.

### 9.7 Multi-currency

**Required concepts:**

- original amount/currency;
- trip base currency;
- FX rate value;
- quote direction;
- provider/source;
- rate timestamp;
- rate type: market, manual, card statement, cash exchange;
- converted amount snapshot.

**Policy khuyến nghị:**

- rate được snapshot tại expense, không tự đổi ngược lịch sử;
- user có thể override bằng tỷ giá thực tế của thẻ/cash;
- base-currency totals là estimate cho đến khi rate được confirmed;
- settlement preview freeze rate set riêng để mọi người cùng thấy một kết quả;
- hiển thị disclaimer: tỷ giá app không phải tỷ giá ngân hàng cuối cùng.

**Offline:** dùng rate cached gần nhất và gắn nhãn “estimated”; cho phép chỉnh sau.

### 9.8 Budgets

Levels:

- total trip budget;
- category budget;
- optional per-person budget;
- daily suggested budget.

Formula cơ bản:

```text
remaining_budget = total_budget - actual_spend
remaining_trip_days = max(1, end_date - current_date + 1)
suggested_daily_budget = remaining_budget / remaining_trip_days
```

Phải phân biệt:

- planned/estimated cost;
- committed cost từ booking;
- paid/actual expense;
- refundable amount;
- personal spend có tính vào shared budget hay không.

Không double-count booking và expense liên kết với booking.

### 9.9 Itinerary

**Scope:** day sections và activity cards.

Fields: title, date/time, timezone, place, notes, attendees, owner, estimated cost, actual cost link, reservation link, status.

Actions: add, reorder, duplicate, move day, mark done/cancelled, link expense, open in map.

Route optimizer và full calendar engine nằm ngoài product boundary; app deep-link sang công cụ chuyên dụng.

### 9.10 Places

Purpose: shared shortlist, không phải map discovery engine.

Fields: name, external provider ID, address, coordinates, category, saved by, notes, votes/reactions, status, linked activity.

Flow:

```text
Share/paste map link
→ Resolve metadata when online
→ Save to trip list
→ Vote
→ Add winner to itinerary
```

Fallback offline: lưu URL và user-entered name trước, resolve sau.

### 9.11 Polls

Types:

- single choice;
- multiple choice;
- availability/date;
- yes/no approval.

Settings: deadline, anonymous/open votes, allow changing vote, quorum, owner.

Flow outcome: winning option có CTA “add to itinerary”, “save place” hoặc “assign booking”.

Tránh build discussion thread; share poll vào chat hiện có.

### 9.12 Bookings

Types: flight, lodging, transport, activity, restaurant, insurance, other.

Fields: provider, confirmation code, start/end, timezone, travelers, amount/currency, paid by, refundability, attachments, contact/link, notes.

Connections:

- booking → itinerary item;
- booking → expense;
- booking → responsibility;
- booking → reminder.

Security: confirmation code và documents là sensitive fields; hạn chế log, notification preview và public share.

### 9.13 Packing

Hai list:

- shared: ai mang adapter, medicine, tripod;
- private: đồ cá nhân, chỉ owner thấy.

Fields: item, quantity, assignee, category, status, note.

Có template theo loại trip nhưng không cần marketplace template.

### 9.14 Responsibilities

Task nhẹ trong context chuyến đi, không phải general project manager.

Examples:

- Minh book hotel trước 20/10;
- Linh check visa requirements;
- An mua eSIM;
- Quân mang power adapter.

Fields: title, assignee(s), due date, status, linked object, reminder, created by.

Statuses: open, in progress, done, cancelled. Subtasks, kanban và dependency graph nằm ngoài product boundary.

### 9.15 Plan fund / shared wallet ledger

**Ranh giới bắt buộc:** Plan Fund là **virtual fund ledger**, không giữ tiền thật.

Use cases:

- mọi người góp quỹ trước trip;
- một người giữ cash chung;
- expense được trả từ quỹ;
- app theo dõi ai đã đóng và quỹ còn bao nhiêu.

Entities: fund, contribution, withdrawal/expense allocation, adjustment.

Phải ghi rõ:

> Plan Fund là sổ theo dõi nội bộ, không phải tài khoản ngân hàng hoặc ví lưu ký.

Không nhận/chuyển tiền trong app trước khi giải quyết licensing, KYC/AML, fraud, chargeback và payment compliance.

### 9.16 Offline-first

Core financial actions phải dùng được offline:

- xem dữ liệu đã tải;
- add/edit expense;
- record settlement draft;
- check itinerary/booking;
- tick packing/responsibility;
- upload receipt vào queue.

Mỗi record có local ID ổn định, sync state và version. UI hiển thị “Saved on this device” thay vì error khi không có mạng.

### 9.17 Memories

Scope:

- add photo hoặc link tới shared album;
- attach media to day/place/activity;
- reactions/captions;
- select highlights for recap.

Memories chỉ hỗ trợ selected highlights, compressed uploads hoặc links tới album ngoài; unlimited full-resolution cloud photo storage nằm ngoài product boundary.

### 9.18 Recap

Recap kết hợp financial và experience data:

- route/destinations;
- total spend và spend by category;
- average per person/day;
- most active day;
- top-voted places;
- completed activities;
- selected photos;
- “crew card” và trip duration.

Public share card phải loại bỏ balance, confirmation codes, exact lodging address và private notes theo mặc định.

### 9.19 Activity feed và audit history

Cross-cutting module bắt buộc trong toàn sản phẩm:

- ai tạo/sửa/xóa expense;
- split thay đổi từ gì sang gì;
- settlement được record/reverse;
- member join/leave;
- base currency/budget thay đổi.

Feed thân thiện; audit event chi tiết phục vụ dispute/support. Sensitive values phải được redact trong operational logs.

### 9.20 Search, export và portability

Scope:

- filter expenses theo member/category/date;
- CSV export;
- plan data JSON export cho portability/support;
- receipt export có permission.

Bao gồm full-text search, PDF plan report và accounting-friendly export trong cùng product scope.

---

## 10. Unified product scope

### 10.1 Scope goal

Cho phép một nhóm thật dùng Beluno làm nguồn sự thật chung cho kế hoạch, tiền bạc, trách nhiệm và ký ức mà không cần spreadsheet phụ. Đây là một scope sản phẩm duy nhất; mọi module dưới đây cùng thuộc definition of done.

### 10.2 Identity, groups, plans và collaboration

- email magic link, passkey hoặc Apple/Google sign-in;
- create, edit, duplicate, archive, export và delete plan;
- plan kind, timing mode, dates/timezone, optional place/destination, base currency, status và cover;
- invite link, lightweight web join/view và guest identity có thể claim;
- owner, admin, member, viewer và guest roles;
- participants, RSVP, recurring series, activity feed, audit history, search và notifications;
- reusable crew và plan templates.

### 10.3 Financial system

- add, edit, void/soft-delete expense;
- one hoặc multiple payers;
- equal, exact, percentage, shares, subgroup, itemized và default split templates;
- categories, receipts, refunds, prepaid và personal/non-shared expenses;
- balances theo member/currency và explainable breakdown;
- debt simplification, partial settlement, reversal và no-settlement-needed;
- original currency, base conversion snapshot, manual FX override và frozen settlement rate;
- total, category, per-person và daily budgets;
- planned, committed và actual spend không double-count;
- virtual plan fund với contributions, withdrawals và adjustments; không giữ tiền thật.

### 10.4 Planning và coordination

- itinerary theo ngày, timezone, attendee, owner và linked cost;
- shared places shortlist, map-link resolver và voting;
- polls cho choice, approval và availability;
- bookings, attachments, travelers, refundability và reminders;
- packing lists riêng/chung;
- responsibilities, assignee, due date, status và reminder;
- liên kết hai chiều expense ↔ booking/activity/place/responsibility;
- Overview thích ứng theo trạng thái plan.

### 10.5 Offline, media và lifecycle

- local-first read/write cho dữ liệu plan thiết yếu;
- durable outbox, idempotency, delta sync, tombstone và visible sync state;
- conflict handling theo loại entity và upload queue cho attachment;
- selected memories, compressed media hoặc external album links;
- recap gồm timeline/route, highlights, plan stats và privacy-safe share card;
- CSV, JSON, PDF report và accounting-friendly export;
- account/plan data export, deletion và retention controls.

### 10.6 Product operations

- onboarding cho organizer, invitee và guest;
- permission timing, notification preferences và digest;
- freemium, Plan Pass và Pro entitlements không khóa collaboration cơ bản;
- analytics có consent và không gửi raw financial text/PII;
- crash, sync, security và cost observability;
- support diagnostics, feature flags, incident runbook và restore workflow;
- localization English/Vietnamese và accessibility baseline.

### 10.7 Critical end-to-end flows

1. Create plan → invite → members join trên app hoặc web.
2. Add booking/place/poll → chốt itinerary → gán responsibility và packing item.
3. Add equal/custom/itemized expense offline → reconnect → mọi device converge.
4. Link booking/activity với planned/actual cost → budget recompute không double-count.
5. Contribute vào virtual plan fund → pay expense from fund → reconcile balance.
6. Edit/void/refund expense → audit event → balances recompute.
7. Preview settlement → record partial/full payment → reverse khi cần.
8. Attach selected memories → complete plan → generate private recap/share card.
9. Search, export ledger/report, duplicate plan và reuse group.
10. Delete/export account data theo privacy lifecycle.

### 10.8 Unified acceptance criteria

- Tất cả module 10.2–10.6 có UX hoàn chỉnh, authorization, analytics, empty/error/offline states và tài liệu hỗ trợ.
- 100% money calculations dùng integer minor units; property tests giữ balance-sum invariant.
- Không mất acknowledged write sau các offline/online transition trong test matrix.
- P95 local save dưới 300 ms trên mid-range device; sync success trong 60 giây sau khi mạng phục hồi đạt mục tiêu SLO.
- User mới tạo trip và hành động đầu tiên trong dưới 3 phút; invitee join và thấy trip trong dưới 60 giây.
- Mọi financial edit có actor, timestamp, previous/current representation và reversal path.
- Booking, budget, expense và fund không double-count trong projection.
- Public recap dùng explicit allowlist, không lộ balance, confirmation code, địa chỉ lưu trú hoặc private note.
- Store listing, privacy policy, account deletion, data export, support và incident runbook sẵn sàng trước public launch.

### 10.9 Explicitly out of product scope

- chat/DM và social network;
- booking marketplace/OTA và route-by-route navigation;
- real-money custody hoặc in-app money transfer;
- unlimited full-resolution photo storage;
- live flight operations;
- enterprise expense management;
- gamification tiền bạc;
- general-purpose project management;
- microservices chỉ để phân tách tổ chức.

---

## 11. Unified delivery model và workstreams

### 11.1 Nguyên tắc single-scope

Beluno có **một product scope và một definition of done** tại Mục 10. Các workstream dưới đây chỉ là cách tổ chức thực thi song song và quản lý phụ thuộc. Chúng không đại diện cho các phiên bản sản phẩm, gói feature, hay quyền loại bỏ module khỏi launch scope.

### 11.2 Workstream A — Product foundation

- identity, auth, role model, group/plan/participant/invite;
- information architecture, design system, accessibility và localization;
- relational schema, local database, migration và environment/CI;
- privacy controls, feature flags và operational telemetry.

### 11.3 Workstream B — Financial truth

- money model, split engine, expenses và receipts;
- balances, debt simplification, settlement và audit;
- multi-currency snapshots, manual override và FX review;
- budgets, planned/committed/actual spend và virtual plan fund.

### 11.4 Workstream C — Planning và coordination

- itinerary, places, polls và bookings;
- packing và responsibilities;
- object linking, phase-aware Overview, reminders và calendar export/sync;
- lightweight web participation.

### 11.5 Workstream D — Reliability và lifecycle

- local-first repository, outbox, pull cursor, idempotency và tombstones;
- attachment upload queue, conflict UX và background jobs;
- memories, recap, search, exports và crew/plan reuse;
- backup/restore, account deletion và support diagnostics.

### 11.6 Workstream E — Growth, revenue và launch

- onboarding, invitation loop và contextual education;
- entitlements, Plan Pass/Pro purchase flow và restore purchase;
- analytics dashboards, ASO/content assets và lifecycle messaging;
- beta operations, app-store readiness, support và incident response.

### 11.7 Dependency order, không phải scope split

```text
Foundation
├── Financial truth ───────┐
├── Planning/coordination ─┼─→ Cross-module integration
└── Offline/sync ──────────┘             ↓
                              Security + full regression
                                         ↓
                              Unified release candidate
```

- Schema và authorization contracts phải ổn định trước khi UI phụ thuộc vào chúng.
- Financial engine và sync invariants cần hoàn thiện trước cross-module budget/fund projections.
- Planning objects cần stable IDs trước khi tạo expense/booking/activity links.
- Memories/recap cần plan lifecycle và privacy allowlist.
- Monetization chỉ bật sau khi entitlement tests chứng minh free collaboration không bị chặn.
- Internal builds có thể xuất hiện thường xuyên để tích hợp và kiểm thử, nhưng public launch chỉ được coi là hoàn tất khi toàn bộ unified scope qua release gates.

### 11.8 Integrated outcome gates

- Ledger, planning, coordination, media và recap hoạt động trên cùng một plan graph.
- Ít nhất 20 completed real plans đi qua create → plan → spend → settle → recap.
- ≥70% activated plans có từ hai active members; invite conversion và planning adoption đạt target.
- Không có known data-loss/security issue; calculation incident ở dưới ngưỡng vận hành.
- Paid entitlement không làm giảm join/contribution rate đáng kể.
- Support load, storage/third-party cost và restore capability nằm trong ngưỡng team vận hành được.

---

## 12. Monetization

### 12.1 Nguyên tắc

- Không khóa khả năng xem mình nợ bao nhiêu hoặc settle cơ bản.
- Không làm invitee phải trả tiền để cộng tác; điều này giết growth loop.
- Charge organizer/power user cho automation, control, history và advanced tools.
- Test willingness to pay sau khi core ledger được tin tưởng.

### 12.2 Recommended model — Freemium + Plan Pass

#### Free

- unlimited joins;
- 1–2 active plans;
- core expenses/splits/balances/settlement;
- limited attachments;
- base budget;
- basic export.

#### Plan Pass

One-time purchase cho một trip, phù hợp user không đi thường xuyên:

- unlimited members/expenses/attachments trong trip;
- advanced FX controls;
- itinerary/bookings/polls/packing;
- full recap;
- enhanced export.

#### Pro subscription

Cho frequent traveler/organizer:

- unlimited active plans;
- advanced split templates;
- receipt attachments, itemized entry và advanced receipt organization;
- full offline packs;
- multi-plan stats;
- duplicate plan/group presets;
- premium recap themes;
- priority support.

### 12.3 Pricing hypotheses cần test

Không coi đây là giá cuối:

- Plan Pass: US$4.99–9.99/trip;
- Individual Pro: US$24.99–39.99/year;
- Family/Crew: US$39.99–59.99/year.

Điều chỉnh theo thị trường và purchasing power. Test price page/interview trước khi xây paywall phức tạp.

### 12.4 Alternative revenue

- affiliate booking chỉ khi không làm méo recommendation;
- paid templates/content packs có value thật;
- B2B small-offsite tier chỉ được xem là mô hình giá cho cùng capability set, không tạo một product scope riêng;
- không bán dữ liệu plan/location cho quảng cáo.

### 12.5 Paywall moments

Paywall hợp lý:

- organizer tạo trip thứ ba;
- cần advanced FX reconciliation;
- thêm nhiều attachment;
- export report đẹp;
- mở full recap;
- dùng booking import hoặc advanced receipt organization.

Paywall không hợp lý:

- invitee vừa mở link;
- user đang cần xem balance;
- user cần settle khoản nợ;
- offline giữa chuyến đi;
- account deletion/data export.

---

## 13. Growth loops

### 13.1 Invite loop

```text
Organizer creates trip
→ Invites 3–8 people
→ Members experience value
→ One member organizes a future trip
→ Creates another trip and invites another group
```

Tối ưu bằng deep link, preview rõ và web fallback.

### 13.2 Settlement loop

Settlement message có context nhưng không shame:

```text
Japan 2027 is ready to settle.
Your final balance: ¥18,500 owed to Minh.
[Review details]
```

Recipient mở app/link, thấy tính minh bạch, trở thành future organizer.

### 13.3 Recap loop

Public-safe recap card được share lên story/group chat:

- destination;
- days;
- crew count;
- highlights;
- fun aggregate stats.

Không hiển thị nợ cá nhân. CTA nhẹ: “Made with Beluno”.

### 13.4 Template/content loop

SEO/ASO content:

- Japan group trip budget template;
- Europe multi-currency trip expense template;
- bachelor trip cost splitter;
- road trip budget calculator.

Template dẫn vào create trip với category/budget defaults.

### 13.5 Crew reuse loop

Sau completed plan:

```text
Create another trip with this crew
→ carry member list
→ optional defaults/templates
→ do not carry balances or sensitive booking data
```

### 13.6 Growth guardrails

- không spam contacts;
- không auto-send notification nếu organizer chưa review;
- invitee biết rõ ai mời và trip nào;
- có rate limit và abuse reporting;
- không dùng dark pattern để upload toàn bộ contacts.

---

## 14. Retention strategy

### 14.1 Hiểu đúng retention

Trip app có tính episodic. D1/D30 retention cá nhân thấp không nhất thiết là thất bại. Cần đo:

- retention trong vòng đời trip;
- crew reuse;
- organizer repeat rate trong 3/6/12 tháng;
- completed plan and settlement rate.

### 14.2 Retention before a plan

- countdown;
- pending decisions;
- due responsibilities;
- budget/booking progress;
- weekly digest chỉ khi có thay đổi hữu ích.

### 14.3 Retention while a plan is active

- Today view;
- quick expense;
- remaining daily budget;
- next booking;
- offline reliability;
- subtle reminder cho unlogged day.

### 14.4 Retention after a plan

- settlement completion;
- recap unlock;
- export;
- memory highlights;
- create next trip with same crew;
- annual travel stats cho Pro.

### 14.5 Anti-retention mistakes

- notification mỗi khi ai đó chỉnh một item nhỏ;
- streak giả tạo không phù hợp travel;
- bắt user mở app để xem thông tin có thể hiển thị an toàn trong notification;
- feed public hoặc gamification tiền bạc gây khó chịu;
- giữ dữ liệu hostage sau khi hết subscription.

---

## 15. Onboarding

### 15.1 Principles

- Explain by doing, không tour 8 màn hình.
- Phân luồng organizer và invitee.
- Không xin notification, contacts, photos và location cùng lúc.
- Cho phép bỏ qua phần không thiết yếu.

### 15.2 Organizer onboarding

```text
Value proposition
→ Create plan (name, dates, base currency)
→ Add first expense OR set budget
→ Generate invite link
→ Home with next best action
```

“Aha” target: thấy một balance thật giữa hai người.

### 15.3 Invitee onboarding

```text
Open invite
→ Preview trip + inviter
→ Continue with name/account
→ Join
→ See “your balance” and next item
```

Không bắt invitee xem product tour. Nếu guest mode chưa đủ an toàn, dùng magic link/passkey/social login tối giản thay vì password flow dài.

### 15.4 Contextual education

- Tooltip split types khi user mở advanced split lần đầu.
- Giải thích rate snapshot khi thêm foreign-currency expense đầu tiên.
- Giải thích settlement preview trước khi record payment.
- Offline banner lần đầu mất mạng, sau đó dùng icon/state gọn.

### 15.5 Permission timing

| Permission | Thời điểm xin |
|---|---|
| Notifications | Sau khi user tạo/join trip và chọn loại reminder |
| Camera/photos | Khi chụp hoặc attach receipt |
| Contacts | Khi user chủ động chọn “Invite from contacts” |
| Location | Khi add place/current location; không cần cho core expense |
| Calendar | Khi user chủ động bật calendar sync/export |

---

## 16. Notifications

### 16.1 Notification taxonomy

#### Transactional — mặc định bật

- invite accepted;
- expense lớn liên quan trực tiếp đến user;
- split của user bị thay đổi;
- settlement recorded/reversed;
- role/ownership changed;
- security event.

#### Actionable reminders — opt-in/configurable

- responsibility due;
- poll closing;
- booking/check-in upcoming;
- trip ready to settle;
- ledger review requested.

#### Digest — opt-in

- weekly planning summary;
- daily in-trip summary;
- post-trip recap ready.

#### Marketing — separate consent

- product updates;
- travel content;
- offers.

### 16.2 Rules

- group burst events thành một digest;
- respect local timezone và quiet hours;
- per-trip mute;
- do not expose confirmation code, exact balance hoặc lodging address trên lock screen theo mặc định;
- email fallback cho high-value account/security events;
- notification deep-link tới đúng object;
- every notification có idempotency key để tránh duplicate.

### 16.3 Tone

Neutral, không shame:

- Nên: “Settlement for Japan 2027 is ready to review.”
- Tránh: “Bạn vẫn chưa trả Minh!”

---

## 17. Edge cases và business rules

### 17.1 Money and rounding

- zero-decimal và three-decimal currencies;
- amount rất lớn;
- percentage tạo remainder;
- expense 1 minor unit chia cho nhiều người;
- refund một phần/toàn bộ;
- chargeback;
- tip/tax không chia đều;
- payer và participant là hai tập khác nhau;
- một người trả bằng hai phương thức/currency;
- expense date khác booking date và card posting date.

Rounding phải deterministic. Ví dụ chia remainder theo thứ tự participant ID ổn định hoặc largest-remainder method; UI phải cho biết ai nhận 1 minor unit dư.

### 17.2 Membership

- member join sau khi đã có expense;
- member rời trước settle;
- removed member vẫn có balance;
- duplicate guest identities;
- guest claim nhầm account;
- owner bị mất account;
- owner muốn delete plan có outstanding balances;
- same person được invite bằng email và link.

### 17.3 Currency

- không có rate cho ngày cũ;
- offline rate stale;
- rate provider outage;
- user đổi base currency giữa trip;
- user override rate sau khi người khác đã settle;
- card dùng dynamic currency conversion;
- cash exchange có phí;
- settlement bằng currency thứ ba.

Rule: thay đổi FX có ảnh hưởng settled entries phải tạo cảnh báo và settlement recalculation event, không silently rewrite.

### 17.4 Concurrent editing

- hai người sửa cùng expense;
- một người delete trong lúc người khác edit;
- cùng tick packing item;
- device offline nhiều ngày với schema version cũ;
- retry request sau timeout tạo duplicate;
- local time/device clock sai.

Financial object conflict không nên “last write wins” mù. Cần version check và explicit resolution hoặc immutable revision.

### 17.5 Dates and timezones

- red-eye flight qua ngày;
- trip qua nhiều timezone;
- daylight-saving transition;
- activity không có time cụ thể;
- member join từ timezone khác;
- booking time hiển thị theo origin/destination local time.

Store UTC instant + IANA timezone khi event có thời gian; date-only giữ kiểu date riêng, không ép midnight UTC.

### 17.6 Budget

- booking estimate sau đó trở thành expense;
- refund làm ngân sách tăng lại;
- personal expense có/không tính vào total;
- expense ngoài trip dates;
- trip end date kéo dài;
- category budget tổng lớn hơn trip budget;
- overspend và negative remaining budget.

### 17.7 Settlement

- partial payment;
- overpayment;
- record nhầm rồi reverse;
- bank fee;
- một member waived debt;
- settle per currency;
- balance dưới tolerance;
- member không đồng ý final ledger.

### 17.8 Files and receipts

- upload offline;
- HEIC/PDF/large image;
- malware hoặc unsafe file;
- EXIF location;
- attachment deleted nhưng expense giữ;
- storage quota;
- signed URL expired.

### 17.9 Abuse and safety

- invite link bị public;
- spam join;
- member sửa/xóa expense để gian lận;
- abusive display name/content;
- scrape private trip;
- enumeration bằng short code;
- notification harassment.

---

## 18. Security và privacy

### 18.1 Threat model tóm tắt

Sensitive assets:

- balances và financial history;
- email/identity;
- itinerary và travel dates;
- lodging/location;
- booking confirmation codes;
- receipts/passport-like documents do user upload nhầm;
- invite tokens;
- payment notes.

Threat actors:

- outsider có invite link;
- removed member;
- malicious current member;
- compromised account/device;
- insider/ops access;
- automated bot.

### 18.2 Authentication

- OAuth/OIDC hoặc passwordless magic link/passkey;
- PKCE cho mobile;
- short-lived access token, secure refresh flow;
- store credentials trong Keychain/Keystore;
- rate limit login/invite attempts;
- optional MFA cho account có sensitive documents;
- device/session management và revoke.

### 18.3 Authorization

- enforce server-side membership cho mọi trip object;
- row-level security nếu dùng Supabase/Postgres nhưng vẫn test policy như code;
- deny by default;
- role/action matrix rõ;
- removed/left member mất access ngay;
- signed, short-lived attachment URLs;
- không tin plan/participant IDs từ client.

### 18.4 Invite security

- random high-entropy token, không dùng mã tăng dần;
- token hash ở database nếu khả thi;
- expiration và revoke/rotate;
- optional approval/usage limit;
- preview không lộ sensitive plan data;
- log join/revoke events;
- rate limit và bot protection cho public endpoint.

### 18.5 Data protection

- TLS in transit;
- encryption at rest từ managed provider;
- application-level encryption cho confirmation codes/documents nếu risk model yêu cầu;
- redact secrets/PII trong logs;
- separate production/staging;
- least-privilege service roles;
- secrets manager, rotation và audit access;
- encrypted backups và restore drills.

### 18.6 Privacy by design

- trip private by default;
- granular visibility cho private packing notes/memories;
- explicit consent trước contact/photo/location access;
- strip EXIF khi phù hợp;
- no ad-tech SDK thu location/financial data;
- analytics event không chứa description, confirmation code, receipt text hoặc exact balance;
- public recap dùng allowlist fields, không phải blocklist.

### 18.7 User rights and lifecycle

- export data;
- delete account;
- leave trip;
- delete/archive plan;
- retention policy cho backups/logs;
- legal hold chỉ khi thực sự cần;
- privacy policy mô tả processor/subprocessor;
- quy trình security incident và user notification.

### 18.8 Financial boundary

Vì app chỉ ghi nhận khoản chi và payment ngoài app, product copy phải tránh ngụ ý đang giữ hoặc chuyển tiền. Real-money transfer nằm ngoài product boundary; mọi đề xuất thay đổi boundary này phải qua review pháp lý theo từng thị trường trước khi code.

---

## 19. Analytics và KPIs

### 19.1 Event design principles

- event names versioned và documented;
- không gửi raw PII/financial text;
- amount có thể bucket hoặc normalize nếu cần product analytics;
- server là nguồn cho authoritative financial events;
- client event có anonymous/session IDs và consent phù hợp;
- distinguish intended action, local commit và server sync success.

### 19.2 Acquisition metrics

- store page conversion;
- invite open → join conversion;
- organic/paid/creator source;
- cost per activated trip, không chỉ cost per install;
- share-to-new-organizer conversion.

### 19.3 Activation funnel

```text
Install/open
→ Sign in or join
→ Create/join trip
→ 2nd member active
→ 1st expense
→ 3 expenses from 2+ members
→ Balance viewed
```

**Activated trip đề xuất:** trong 72 giờ có ≥2 active members và ≥3 expenses, trong đó ≥2 members đã tạo hoặc xác nhận hành động.

### 19.4 Engagement metrics

- expenses per active plan/day;
- % expenses added offline;
- time to add expense;
- active collaborators per trip;
- split type distribution;
- budget adoption;
- plan module adoption;
- notification open-to-action rate;
- sync conflict and retry rate.

### 19.5 Completion and trust metrics

- trip settlement rate;
- time from end date to settled;
- % plans with unresolved balance after 14 days;
- expense edit/dispute rate;
- balance calculation support tickets;
- data-loss incidents;
- export usage;
- CSAT after settlement.

### 19.6 Retention metrics

- organizer repeat trip rate at 90/180/365 days;
- same-crew reuse rate;
- active-through-trip rate;
- planning-adopter lift so với plans không dùng planning modules;
- recap share rate;
- subscription renewal.

### 19.7 Monetization metrics

- paywall view → purchase;
- free-to-paid organizer conversion;
- Plan Pass attach rate;
- ARPPU and revenue per completed plan;
- trial-to-paid and churn;
- refund rate;
- gross margin after FX/map/storage/notification costs.

### 19.8 Quality SLOs

- API availability target: 99.9% after public launch;
- crash-free sessions >99.5%;
- sync success within 60 seconds after network restoration >99%;
- duplicate financial record rate effectively zero;
- P95 read API <500 ms in primary region;
- P95 server write <800 ms, while local save remains near-instant.

### 19.9 Dashboard cadence

- daily: crash, sync, auth, duplicate/error alerts;
- weekly: activation, WAP-MC, invite conversion, completed plans;
- monthly: repeat organizers, monetization, cohorts, support themes;
- per release: regression comparison và guardrail metrics.

---

## 20. Technical architecture

### 20.1 Recommended shape

**Modular monolith + managed infrastructure**, không microservices.

```text
Mobile app (iOS/Android)
├── UI / state
├── domain modules
├── local SQLite database
├── outbox + sync engine
└── secure credential store
        │
        ▼
API / Backend-for-Frontend
├── auth and authorization
├── group/plan/participant service
├── ledger/split/balance service
├── sync endpoints
├── notifications
└── export/background jobs
        │
        ▼
PostgreSQL + object storage + queue/jobs
```

### 20.2 Domain boundaries

- **Identity:** users, sessions, guest claim.
- **Groups & Plans:** reusable groups, plan lifecycle, participants, RSVP, roles, invites.
- **Ledger:** expenses, payers, splits, FX, balances, settlements.
- **Budgeting:** planned/committed/actual totals.
- **Planning:** itinerary, places, polls, bookings.
- **Coordination:** packing, responsibilities, notifications.
- **Media:** attachments, receipts, memories.
- **Sync/Audit:** revisions, tombstones, outbox acknowledgements.
- **Analytics:** privacy-safe events.

Giữ module boundaries trong code/database dù deploy chung.

### 20.3 Authoritative calculations

- Client tính optimistic preview để UI nhanh.
- Server validate và là authority cho financial write.
- Balance projection có thể materialize để đọc nhanh nhưng phải rebuild được từ ledger.
- Mọi write tài chính chạy trong database transaction.
- Idempotency keys ngăn duplicate do retry.

### 20.4 Read model

Không query lại toàn bộ ledger trên mobile cho mỗi màn hình. Có thể dùng:

- `member_balances` materialized/projection table;
- `plan_spend_summary` theo category/currency/day;
- incremental recompute trong cùng transaction hoặc background job đáng tin cậy;
- reconciliation job định kỳ so projection với source ledger.

### 20.5 Background jobs

- push/email notification;
- attachment processing/virus scan;
- export generation;
- FX rate ingest/cache;
- recap generation;
- stale invite cleanup;
- projection reconciliation;
- account/plan deletion workflow.

### 20.6 Observability

- structured logs có correlation/request ID;
- error tracking với release version;
- sync metrics: queue age, retries, conflicts;
- audit access logs cho sensitive operations;
- dashboards và alerting cho auth/financial invariants;
- no raw receipt or sensitive note in logs.

---

## 21. Suggested stack

### 21.1 Default recommendation cho solo/small team

| Layer | Recommendation | Vì sao |
|---|---|---|
| Mobile | Flutter + Dart | Một codebase iOS/Android, UI nhất quán, SQLite/Drift mạnh |
| Local data | SQLite + Drift | Typed queries, migrations, reactive local-first UI |
| State | Riverpod | Dependency/state management rõ, testable |
| Navigation | go_router | Deep link và nested navigation |
| Backend API | Python + FastAPI + Pydantic | Async REST/OpenAPI rõ, validation tốt, modular monolith dễ tổ chức |
| Database | PostgreSQL | Transactions, constraints, RLS, reporting tốt |
| Data access | SQLAlchemy async + Psycopg | Typed mappings/queries, transaction control và PostgreSQL support |
| Migrations | Alembic + reviewed PostgreSQL SQL | Expand-contract migrations; giữ quyền kiểm soát trigger/RLS/constraint |
| Storage | Supabase Storage/S3-compatible | Signed URL, lifecycle policy |
| Jobs | Procrastinate + transactional application outbox | PostgreSQL-backed queue, retry/locks/periodic tasks, không cần Redis ban đầu |
| Push | FCM + APNs abstraction | Cross-platform push |
| Analytics | PostHog/Amplitude với privacy schema | Funnels/cohorts; self-host option nếu cần |
| Errors | Sentry/Crashlytics | Crash, release và performance monitoring |
| Web | Lightweight web client theo stack frontend được chọn | Invite landing, legal, join/view/vote |
| CI/CD | GitHub Actions + store automation | Tests/build/signing/release consistency |

### 21.2 Python quality toolchain

- uv + locked dependencies;
- Ruff format/lint;
- mypy strict type checks;
- pytest/pytest-asyncio;
- Hypothesis cho money/sync state machines;
- Testcontainers với PostgreSQL thật;
- Schemathesis cho OpenAPI contract fuzzing;
- Locust cho load testing.

### 21.3 Build-vs-buy decisions

**Buy/managed:** auth, object storage, push transport, crash reporting, FX source, email delivery.  
**Build:** group/plan domain, ledger rules, split engine, balance explanation, settlement và offline conflict policy.

### 21.4 Stack guardrails

- Không dùng Firebase-only document model nếu relational ledger và transactional constraints trở nên khó quản lý.
- Không thêm event bus/Kafka khi modular monolith đáp ứng transaction và throughput.
- Không build custom auth.
- Không chọn sync framework chỉ vì demo nhanh; test tombstone, conflict, schema migration và RLS trước.
- Pin currency metadata and money library behavior bằng tests.

---

## 22. Database schema

### 22.1 Conventions

- IDs: UUIDv7/ULID để sortable và có thể tạo client-side.
- Money: `BIGINT amount_minor` + `CHAR(3) currency_code`.
- Time: `TIMESTAMPTZ`; date-only dùng `DATE`; timezone dùng IANA string.
- Soft delete: `deleted_at` + tombstone trong sync scope.
- Concurrency: `version BIGINT` hoặc revision token.
- Audit: `created_at`, `created_by`, `updated_at`, `updated_by`.
- Multi-tenant scope: hầu hết tables có `plan_id` để authorization/index dễ.

### 22.2 Core identity and plan entities

#### `users`

- `id`
- `email_normalized`
- `display_name`
- `avatar_url`
- `locale`
- `default_currency`
- `status`
- timestamps

#### `plans`

- `id`
- `owner_user_id`
- `group_id` nullable
- `title`, `plan_kind`
- `start_date`, `end_date`
- `time_mode`
- `timezone`
- `base_currency`
- `status`
- `total_budget_minor` nullable
- `cover_asset_id` nullable
- `version`, timestamps, `deleted_at`

#### `travel_plan_destinations`

- `id`, `plan_id`
- `name`, `country_code`
- `latitude`, `longitude` nullable
- `start_date`, `end_date` nullable
- `sort_order`

#### `plan_participants`

- `id`, `plan_id`
- `user_id` nullable cho guest chưa claim
- `guest_identity_id` nullable
- `display_name_snapshot`
- `role`, `access_status`
- `rsvp_status`: invited/going/maybe/declined
- `default_share_weight`
- `joined_at`, `left_at`
- unique identity within plan

#### `plan_series`

- `id`, optional `group_id`
- recurrence rule + IANA timezone
- materialization horizon
- lifecycle/version metadata
- each occurrence is an independent `plan`

#### `plan_invites`

- `id`, `plan_id`, `created_by`
- `token_hash`
- `role_granted`
- `expires_at`, `max_uses`, `use_count`
- `requires_approval`
- `revoked_at`

### 22.3 Financial ledger entities

#### `expenses`

- `id`, `plan_id`
- `description`
- `expense_type`: expense/refund/adjustment
- `amount_minor`
- `currency_code`
- `expense_date`
- `category_id`
- `place_id`, `booking_id`, `itinerary_item_id` nullable
- `fx_rate_id` nullable
- `base_amount_minor_snapshot` nullable
- `notes_ciphertext` or `notes`
- `status`: active/voided
- revision/audit fields

#### `expense_payers`

- `id`, `plan_id`, `expense_id`
- `member_id`
- `amount_minor`
- constraint: sum payer amounts = expense amount

#### `expense_splits`

- `id`, `plan_id`, `expense_id`
- `member_id`
- `owed_minor`
- `split_method`
- `input_value` nullable: percent/share/exact
- constraint: sum owed = expense amount

#### `fx_rates`

- `id`, `plan_id` nullable
- `base_currency`, `quote_currency`
- `rate_decimal` high precision
- `rate_timestamp`
- `source`
- `rate_type`
- `created_by` nullable
- unique provider/timestamp pair as appropriate

#### `settlements`

- `id`, `plan_id`
- `from_member_id`, `to_member_id`
- `amount_minor`, `currency_code`
- `base_amount_minor_snapshot` nullable
- `fx_rate_id` nullable
- `payment_method_label`
- `paid_at`
- `status`: recorded/reversed
- `reverses_settlement_id` nullable
- audit fields

#### `member_balances`

- `plan_id`, `member_id`, `currency_code`
- `net_minor`
- `calculated_at`
- `ledger_revision`
- primary/unique key on plan/participant/currency

Đây là projection/cache, không phải source of truth.

### 22.4 Budget entities

#### `budget_categories`

- `id`, `plan_id`
- `name`, `icon`, `color_token`
- `budget_minor` nullable
- `currency_code` = trip base currency
- `sort_order`, `archived_at`

#### `planned_costs`

- `id`, `plan_id`
- `category_id`
- `source_type`, `source_id` nullable
- `estimate_minor`, `currency_code`
- `status`: estimated/committed/converted_to_expense/cancelled

### 22.5 Planning entities

#### `itinerary_items`

- `id`, `plan_id`
- `title`, `description`
- `start_at`, `end_at`, `timezone`
- `date_only` support
- `place_id`, `booking_id`
- `owner_member_id`
- `status`, `sort_order`
- `estimated_cost_minor`, `currency_code`

#### `itinerary_attendees`

- `itinerary_item_id`, `member_id`
- `status`: invited/going/not_going/maybe

#### `places`

- `id`, `plan_id`
- `name`, `address`
- `latitude`, `longitude`
- `provider`, `provider_place_id`
- `external_url`, `category`
- `added_by`, `status`

#### `polls`, `poll_options`, `poll_votes`

- poll: question, type, settings, deadline, status;
- option: label, linked entity, sort order;
- vote: member, option, value, timestamps;
- unique vote constraints depend on poll type.

#### `bookings`

- `id`, `plan_id`, `type`
- `provider_name`
- `confirmation_code_ciphertext`
- `start_at`, `end_at`, `timezone`
- `amount_minor`, `currency_code`
- `paid_by_member_id`
- `status`, `refundability`
- `source`, `source_message_id` nullable

#### `booking_travelers`

- `booking_id`, `member_id`

### 22.6 Coordination and media entities

#### `packing_items`

- `id`, `plan_id`, `owner_user_id` nullable cho private item
- `visibility`: trip/private
- `title`, `category`, `quantity`
- `assignee_member_id`, `status`

#### `responsibilities`

- `id`, `plan_id`
- `title`, `assignee_member_id`
- `due_at`, `status`
- `linked_type`, `linked_id`

#### `plan_funds`, `fund_entries`

- fund: currency, custodian_member_id, status;
- entry: contribution/expense/withdrawal/adjustment, member, amount, linked expense.

#### `attachments`

- `id`, `plan_id`
- `owner_type`, `owner_id`
- `storage_key`
- `mime_type`, `size_bytes`, `checksum`
- `scan_status`
- `visibility`
- timestamps

#### `memories`

- `id`, `plan_id`
- `attachment_id` hoặc external URL
- `itinerary_item_id`, `place_id`
- `caption`, `captured_at`, `added_by`
- `selected_for_recap`

### 22.7 Sync and audit entities

#### `change_log`

- monotonically increasing `server_seq`
- `plan_id`
- `entity_type`, `entity_id`
- `operation`: upsert/delete
- `entity_version`
- `changed_at`
- minimal payload or pointer

#### `idempotency_keys`

- `actor_id`, `key`, `endpoint_scope`
- `request_hash`
- `response_status`, `response_body_ref`
- `expires_at`

#### `audit_events`

- `id`, `plan_id`
- `actor_user_id/member_id`
- `action`
- `entity_type`, `entity_id`
- `before_json`, `after_json` with sensitive-field redaction/encryption
- `occurred_at`, `request_id`, `device_id`

#### `device_sync_state`

- `device_id`, `user_id`, `plan_id`
- `last_pulled_server_seq`
- `last_seen_at`
- `client_schema_version`

### 22.8 Relationship map

```text
User ──< GroupMember >── Group
Group ──< Plan ──< PlanParticipant >── User/Guest
Plan ──< Expense ──< ExpensePayer >── PlanParticipant
Plan ──< Expense ──< ExpenseSplit >── PlanParticipant
Expense >── FxRate
Plan ──< Settlement >── PlanParticipant (from/to)
Plan ──< BudgetCategory ──< Expense
Plan ──< ItineraryItem >── Place
ItineraryItem ──< ItineraryAttendee >── PlanParticipant
Booking ──> ItineraryItem / Expense / Responsibility
Poll ──< PollOption ──< PollVote >── PlanParticipant
Plan ──< Attachment ──> Expense/Booking/Memory
Plan ──< AuditEvent
```

### 22.9 Essential indexes

- `(plan_id, updated_at)` và `(plan_id, deleted_at)` cho sync;
- `(plan_id, expense_date desc)`;
- `(plan_id, member_id, currency_code)` balances;
- unique `(expense_id, member_id)` payer/split khi một row mỗi member;
- `(plan_id, start_at)` itinerary/bookings;
- unique invite token hash;
- `change_log(plan_id, server_seq)`;
- partial indexes cho active/non-deleted rows.

---

## 23. API considerations

### 23.1 API style

REST/JSON phù hợp với domain và giúp sync dễ quan sát. Có thể dùng typed OpenAPI contract. GraphQL không cần thiết nếu chưa có client/query complexity thật.

### 23.2 Resource examples

```text
POST   /v1/plans
GET    /v1/plans/{planId}
PATCH  /v1/plans/{planId}
POST   /v1/plans/{planId}/invites
POST   /v1/invites/{token}/join

POST   /v1/plans/{planId}/expenses
PATCH  /v1/plans/{planId}/expenses/{expenseId}
DELETE /v1/plans/{planId}/expenses/{expenseId}
GET    /v1/plans/{planId}/balances

POST   /v1/plans/{planId}/settlement-previews
POST   /v1/plans/{planId}/settlements
POST   /v1/plans/{planId}/settlements/{id}/reverse

POST   /v1/sync/push
GET    /v1/sync/pull?scope=plan:{planId}&after=serverSeq
```

### 23.3 Financial write contract

Một expense create request nên gửi đầy đủ:

- client-generated `expense_id`;
- idempotency key;
- amount/currency;
- payer allocation;
- split allocation hoặc method inputs;
- FX snapshot/override metadata;
- client timestamp và device ID;
- base entity version nếu edit.

Server:

1. authorize actor;
2. validate members belong to trip;
3. validate sums and money scale;
4. resolve/freeze FX policy;
5. write expense/payers/splits/audit atomically;
6. update projection/change log;
7. return authoritative representation and server sequence.

### 23.4 Idempotency

Mọi POST tạo financial record cần `Idempotency-Key`. Cùng key + cùng payload trả lại response cũ. Cùng key + payload khác trả conflict.

### 23.5 Optimistic concurrency

- include `version`/ETag;
- PATCH dùng `If-Match` hoặc expected version;
- mismatch trả `409 Conflict` với current server snapshot;
- client cho user chọn review, đặc biệt với expense;
- toggle/checklist có thể merge đơn giản hơn.

### 23.6 Error model

Consistent error body:

```json
{
  "code": "SPLIT_TOTAL_MISMATCH",
  "message": "Split amounts must equal the expense total.",
  "details": {"expectedMinor": 12000, "actualMinor": 11999},
  "requestId": "...",
  "retryable": false
}
```

Không trả stack trace hoặc sensitive server detail.

### 23.7 Pagination and filtering

- cursor pagination;
- stable ordering by date + ID;
- filters: member, category, currency, date, sync revision;
- avoid offset pagination cho activity/expense feed lớn.

### 23.8 Versioning and compatibility

- `/v1` cho breaking API contract;
- additive fields mặc định backward-compatible;
- minimum supported client version chỉ dùng khi migration/security bắt buộc;
- server biết client schema version trong sync handshake;
- feature flags cho staged rollout.

### 23.9 Rate limits

Theo actor/IP/trip cho:

- auth/invite join;
- create expense bursts;
- upload;
- export;
- FX lookup;
- notification-triggering actions.

Rate limit không được làm offline retry queue bị mất; trả `Retry-After`.

---

## 24. Offline-first và sync strategy

### 24.1 Source-of-truth model

- **Local SQLite** là source cho UI.
- **Server Postgres** là authority cross-device.
- Client write local transaction trước, thêm operation vào outbox.
- Sync worker push outbox, nhận acknowledgement và pull changes.

### 24.2 Local record states

- `synced`;
- `pending_create`;
- `pending_update`;
- `pending_delete`;
- `conflict`;
- `failed_retryable`;
- `failed_permanent`.

UI chỉ cần icon/label gọn; chi tiết trong Sync Center/support screen.

### 24.3 Push flow

```text
User action
→ local DB transaction
   ├── update entity
   └── append outbox operation
→ UI updates immediately
→ sync worker batches operations
→ server validates atomically
→ ack with entity version + server_seq
→ client marks synced and pulls newer changes
```

### 24.4 Pull flow

```text
GET changes after last_server_seq
→ apply in one local transaction
→ preserve pending local edits
→ advance cursor only after commit
```

Cursor phải per trip/device hoặc per account scope rõ ràng. Không advance cursor nếu apply thất bại.

### 24.5 Conflict policy by entity type

| Entity | Policy |
|---|---|
| Expense/split/settlement | Version conflict; require explicit merge/retry or immutable revision |
| Trip name/cover | Last-write-wins có audit, nếu product chấp nhận |
| Packing checked state | Last-write-wins hoặc timestamp merge |
| Poll vote | Upsert per voter/option theo poll rule |
| Itinerary ordering | Fractional order key + deterministic rebalance |
| Attachment | Append-only; metadata conflict separate |
| Delete vs edit | Tombstone wins, nhưng surface recover/recreate option |

### 24.6 Deletion and tombstones

- server giữ tombstone đủ lâu cho devices offline quay lại;
- hard delete qua retention job sau grace period;
- deleted financial record giữ audit/reversal semantics;
- client purge local data khi mất membership hoặc account logout theo policy.

### 24.7 Ordering and IDs

- client-generated UUIDv7/ULID tránh chờ server;
- server sequence dùng cho change order, không dùng device time;
- client time chỉ là metadata;
- deterministic sort fallback bằng ID.

### 24.8 Retry strategy

- exponential backoff + jitter;
- respect connectivity and battery;
- retry only retryable errors;
- idempotency key giữ nguyên qua retry;
- attachment upload resumable nếu provider hỗ trợ;
- user có manual retry và export diagnostics không chứa sensitive data.

### 24.9 FX offline policy

- cache rates theo currency/date;
- nếu không có rate đúng ngày, dùng recent rate và mark estimated;
- expense vẫn lưu original amount;
- khi online, không tự thay rate đã user-confirmed;
- unresolved estimate hiện trong review queue trước settle.

### 24.10 Schema migrations

- local migrations forward-only, tested từ vài version gần nhất;
- backup local DB metadata trước risky migration;
- sync handshake từ chối write nếu client schema quá cũ cho invariant mới;
- rollout server additive trước, client sau, cleanup cuối.

---

## 25. Test strategy

### 25.1 Test pyramid

#### Unit tests

- money scale and formatting;
- split algorithms;
- rounding;
- balance aggregation;
- debt simplification;
- FX direction/conversion;
- budget calculations;
- notification eligibility;
- permission decisions.

#### Property-based tests

Generate randomized groups/expenses và verify:

- payer sum = expense total;
- split sum = expense total;
- Σ member net = 0;
- settlement không tạo/huỷ giá trị;
- rounding bounded by minor-unit policy;
- reversing settlement restores previous balance;
- conversion direction round-trip within tolerance.

#### Database/integration tests

- constraints and transactions;
- row-level security/authorization matrix;
- idempotency;
- concurrent edits;
- projection reconciliation;
- delete/tombstone;
- invite expiry/revoke;
- export correctness.

#### Sync tests

- create offline then reconnect;
- edit same record on two devices;
- delete vs edit;
- duplicate retries/timeouts;
- pull interrupted mid-batch;
- device clock wrong;
- offline for 30 days;
- app killed during local/outbox transaction;
- schema upgrade with pending outbox.

#### UI/widget tests

- quick expense form;
- advanced split validation;
- balance explanation;
- offline/sync states;
- deep links/invite;
- accessibility labels và large text;
- localized currency/date layout.

#### End-to-end tests

1. Organizer creates trip and invites member.
2. Two devices add expenses online/offline.
3. Edit split, observe audit and balances.
4. Settle partially then reverse.
5. Budget updates and export matches ledger.
6. Removed member loses access but remains in history.

### 25.2 Money golden cases

Maintain fixture suite:

- 3 people split ¥10;
- 4 people split $10.01;
- multiple payers;
- exact/percentage/share split;
- refund and partial refund;
- JPY/KWD/USD decimals;
- custom FX rate;
- settled and reopened ledger;
- large group and amount boundaries.

### 25.3 Security tests

- IDOR across plans;
- invite token brute force/rate limit;
- removed member session;
- signed URL leakage;
- file upload validation;
- log redaction;
- auth token storage;
- RLS policy regression;
- public recap field allowlist;
- account deletion and backup lifecycle review.

### 25.4 Performance tests

- 100 members/10,000 expenses as upper stress fixture dù typical nhỏ hơn;
- balance rebuild;
- sync delta size;
- cold start with active trip;
- long offline outbox;
- image upload on weak network;
- low-memory Android device.

### 25.5 Manual QA matrix

- iOS current and previous major;
- Android current and 2–3 common older versions depending market;
- low-end Android;
- airplane mode, flaky 3G, captive portal;
- light/dark mode;
- English/Vietnamese theo launch-market decision;
- USD/JPY/VND/KWD;
- accessibility: screen reader, dynamic text, contrast, touch targets.

### 25.6 Unified release gates

- all ledger invariants green;
- no P0/P1 known issues across financial, planning, coordination, offline, media và recap flows;
- crash-free beta sessions target reached;
- migration rehearsal complete;
- privacy/security checklist passed;
- rollback/feature flag plan ready; feature flags dùng để giảm operational risk, không để bỏ module khỏi unified scope;
- production restore test recent.

---

## 26. Launch plan

Launch plan dùng các cohort tăng dần để kiểm soát rủi ro. Mỗi cohort nhận cùng unified product scope; khác biệt chỉ ở số lượng người dùng, mức hỗ trợ và rollout controls, không phải feature set.

### Stage A — Problem validation (1–2 weeks, trước build hoặc song song prototype)

- 12–20 interviews với organizers và frequent payers;
- thu thập artifacts: Sheets, screenshots, Splitwise flows;
- test top pains và willingness to switch;
- clickable prototype cho create → add expense → settle;
- success: ≥60% interviewees đã gặp multi-app/settlement pain và ≥5 nhóm đồng ý beta.

### Stage B — Concierge alpha

- 5–10 nhóm, founder hỗ trợ trực tiếp;
- real plans hoặc simulated weekend trip;
- observe entry speed, confusion và disputes;
- manually inspect sync/calculation anomalies;
- không chạy paid acquisition.

### Stage C — Private beta

- 20–50 plans;
- TestFlight/Internal App Sharing hoặc closed tracks;
- in-app feedback tied to screen/release;
- weekly interview với 3–5 organizers;
- publish known issues và operational limits rõ, không giới thiệu đây là một product scope khác.

### Stage D — Soft launch

- chọn 1–2 thị trường/ngôn ngữ;
- store listing, privacy/legal, support FAQ;
- referral/invite loop live;
- small creator/community seeding;
- monitor activation, sync, crash, support.

### Stage E — Public launch

Chỉ mở rộng khi:

- ledger trust metrics ổn;
- no unresolved data-loss bug;
- invite conversion đủ tốt;
- onboarding usability đạt target;
- support load manageable;
- paywall không cản collaboration.

### Launch channels

- TikTok/Reels demo “chia tiền trip Nhật trong 15 giây”;
- travel Facebook groups/Reddit/community theo policy;
- micro creators về budget travel/group travel;
- SEO calculators/templates;
- Product Hunt/Hacker News chỉ là awareness phụ, không phải core audience;
- campus/backpacking communities;
- partnership với trip organizer content creators.

### Support setup

- help center cho balance, FX, settle, offline;
- “report calculation issue” đính kèm sanitized diagnostic ID;
- status page;
- response playbook cho data/sync incident;
- no request for sensitive screenshots nếu không cần.

---

## 27. ASO và content ideas

### 27.1 Keyword clusters

- split expenses travel;
- group trip planner;
- travel budget app;
- trip expense tracker;
- split bills with friends;
- multi currency expense;
- vacation planner with friends;
- road trip budget;
- who owes whom trip;
- travel spending tracker.

Localize theo ngôn ngữ tự nhiên, không chỉ dịch literal.

### 27.2 Store title/subtitle hypotheses

**Title:** Beluno: Plans with Friends  
**Subtitle:** Decide, split, meet & remember

### 27.3 Screenshot story

1. “Everything your group trip needs.”
2. “Split expenses in seconds.”
3. “See exactly who owes whom.”
4. “Travel across currencies.”
5. “Stay on budget every day.”
6. “Works when the internet doesn’t.”
7. “Plans, bookings and decisions together.”

### 27.4 SEO/content topics

- How to split travel expenses fairly with friends;
- Equal vs percentage vs shares: which split should a group use?;
- Group trip budget template;
- Multi-currency travel expense spreadsheet alternative;
- How to settle group trip costs with fewer transfers;
- Japan/Europe/Thailand trip budget calculators;
- Who should pay upfront for group bookings?;
- Group trip checklist by timeline;
- Travel budget categories people forget;
- How to avoid money arguments on a friend trip.

### 27.5 Interactive acquisition assets

- free web trip cost splitter;
- settlement calculator;
- daily travel budget calculator;
- downloadable packing/budget template;
- “paste expenses, get settlement plan” privacy-safe tool;
- currency-aware trip budget template.

### 27.6 Content principles

- demonstrate real workflow, không chỉ lifestyle imagery;
- use messy real-world cases;
- avoid claiming guaranteed minimum transfers unless algorithm proves it;
- disclose affiliate relationships;
- never publish identifiable user plan data without consent.

---

## 28. Risks và mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---:|---:|---|
| Scope bloat thành super app | High | High | Frozen unified scope; explicit product boundaries; change-control cho yêu cầu ngoài Mục 10 |
| Balance bug làm mất trust | Medium | Critical | Integer money, invariants, property tests, audit, staged rollout |
| Offline sync mất/duplicate expense | Medium | Critical | Local transaction + outbox, idempotency, conflict tests, observability |
| Wanderlog/competitor đã có nhiều feature | High | Medium | Wedge vào trusted financial workflow; UX nhanh; specific segment |
| Invitee không muốn cài app | High | High | Web/deep-link preview, frictionless join, value visible immediately |
| Low repeat frequency | High | Medium | Price per trip, crew reuse, episodic metrics, planning/recap extensions |
| Monetization cản network loop | Medium | High | Free collaboration; charge organizer/power features |
| FX disagreement | Medium | High | Rate snapshot/source/manual override/settlement freeze/explanation |
| Privacy breach lộ itinerary/booking | Low–Medium | Critical | Private default, strict authz, signed URLs, redaction, threat modeling |
| Storage cost từ receipts/photos | Medium | Medium | Quota, compression, lifecycle, highlights-only memories |
| Third-party API cost/outage | Medium | Medium | Cache, fallback/manual mode, provider abstraction, cost caps |
| App stores reject privacy/payment behavior | Low–Medium | High | Early review, accurate disclosures, virtual ledger only; no real-money wallet |
| One organizer does all work | High | Medium | Assignable actions, contributor prompts, passive member UX |
| Users stay with Sheets/Splitwise | High | High | Show integrated budget/offline/FX value within first session |

### 28.1 Kill criteria

Pause/reposition nếu sau 30–50 real plans:

- <30% invited members join;
- <40% plans reach 3 expenses;
- organizers vẫn duy trì spreadsheet song song vì thiếu trust;
- calculation/sync support burden không giảm sau iterations;
- interviewees thích concept nhưng không dùng trong trip thật;
- no clear willingness to pay from repeat organizers.

---

## 29. Product boundaries

Các mục dưới đây **không thuộc unified product scope**. Đây là quyết định định vị và kiểm soát complexity, không phải danh sách feature được trì hoãn.

### 29.1 Chat

Nhóm đã có Messenger/WhatsApp. Chat tạo moderation, notifications và sync complexity nhưng không tạo wedge.

### 29.2 Booking marketplace

Cạnh tranh với OTA, inventory và support quá lớn. Chỉ lưu/link booking.

### 29.3 Route optimization và navigation

Deep-link sang Maps. Routing API tốn phí và không giúp chứng minh financial PMF.

### 29.4 AI itinerary generator

Demo hấp dẫn nhưng dễ làm loãng core; output không phải moat. AI itinerary generator không nằm trong scope đã chốt.

### 29.5 Real-money wallet

Compliance, fraud, KYC/AML, chargeback và licensing vượt scope team nhỏ. Plan Fund chỉ là virtual ledger.

### 29.6 Receipt OCR/itemization

Third-party cost, accuracy và correction UX phức tạp. Scope hỗ trợ manual receipt attachment và itemized entry, không tự động OCR.

### 29.7 Full social network

Không public feed, follows, DMs, likes hoặc discovery profiles.

### 29.8 Unlimited cloud photo album

Storage/bandwidth/support lớn. Dùng selected highlights hoặc external album links.

### 29.9 Live flight operations

Data licensing/API cost cao; TripIt-like live operations không thuộc product positioning.

### 29.10 Desktop admin/back office lớn

Chỉ build internal tools tối thiểu cho support, feature flags và audit. Không tạo CMS chung chung.

### 29.11 Gamification tiền bạc

Không leaderboard “ai trả nhiều nhất/chi ít nhất”, streak trả nợ hoặc badge gây xấu hổ.

### 29.12 Microservices

Modular monolith phù hợp với tải và transaction boundary dự kiến; microservices làm tăng deployment, tracing và consistency cost.

---

## 30. Kế hoạch thực thi single-scope

### 30.1 Capacity reality

Toàn bộ phạm vi ở Mục 10 lớn hơn đáng kể một ứng dụng chia tiền thông thường. Có hai cách thực thi mà không chia nhỏ product scope:

| Mô hình | Nguồn lực | Thời lượng dự kiến | Điều kiện |
|---|---|---:|---|
| Accelerated | 4–6 người: product/design, 2 mobile, backend, QA/automation; một số vai trò có thể kiêm nhiệm | 12 tuần | Làm song song, dùng managed services, scope đã khóa, team senior |
| Small team | 2–3 người full-time | 20–28 tuần | Ít song song hơn, vẫn cùng một definition of done |
| Solo | 1 senior full-stack mobile developer | 28–36 tuần | UI system có sẵn, hạ tầng managed, không đổi scope giữa chừng |

Mốc dưới đây mô tả **phương án accelerated 12 tuần**. Với small team hoặc solo, giữ nguyên deliverables và chạy tuần tự các workstream trong thời lượng tương ứng. Internal builds có thể được phát hành cho tester trong quá trình phát triển, nhưng public launch chỉ xảy ra khi toàn bộ unified scope đạt release gate.

### 30.2 Team topology cho kế hoạch 12 tuần

- **Product/design:** research, flows, design system, content, acceptance.
- **Mobile A:** trip, planning, coordination, media và recap UI.
- **Mobile B:** financial UI, local database, offline/sync và platform integration.
- **Backend:** schema, authz, ledger, sync API, jobs, storage và exports.
- **QA/automation:** test harness, devices, security/regression và release operations.
- Product owner giữ một backlog duy nhất theo Mục 10; không tách module thành product version riêng.

### Week 1 — Scope lock, domain contract và prototype tổng thể

Deliverables:

- 12–20 user interviews hoặc evidence synthesis;
- prototype xuyên suốt create → plan → spend → settle → recap;
- information architecture và screen inventory hoàn chỉnh;
- money invariants, FX policy, permission matrix và privacy classification;
- entity relationship model, API/sync contracts và analytics taxonomy;
- design tokens/components và accessibility baseline;
- beta cohorts cam kết dùng trip thật.

Exit criteria:

- unified scope được ký duyệt;
- không còn quyết định domain P0 chưa có owner;
- prototype vượt qua usability test với organizer và invitee.

### Week 2 — Foundation trên client và server

Build song song:

- repositories, CI/CD, environments và feature flags;
- auth, secure session, user/group/plan/participant/invite/guest claim;
- Postgres schema, RLS/authz và local SQLite migrations;
- app shell, navigation, localization và deep links;
- object storage, signed URL và attachment queue skeleton;
- logging, crash reporting, metrics và audit framework.

Validation:

- authorization matrix tests;
- local/server migration tests;
- invite revoke/expiry/join flow;
- production-like environment smoke test.

### Week 3 — Financial ledger và quick expense

Build:

- integer money model và currency metadata;
- expense, multiple payers, equal/exact/percentage/shares/itemized splits;
- receipt attachment và expense categories;
- quick-add/advanced forms;
- local transaction + outbox;
- atomic server validation, idempotency và audit event.

Validation:

- property-based invariants;
- money golden cases;
- invalid split, duplicate retry và permission tests;
- P95 local save target.

### Week 4 — Balances, settlement, FX và budgets

Build:

- balance projections và explainable drill-down;
- debt simplification, partial/full settlement và reversal;
- FX cache, snapshots, manual override và settlement rate freeze;
- total/category/per-person/daily budgets;
- planned/committed/actual cost model;
- virtual plan fund, contribution và fund-paid expense.

Validation:

- reconciliation rebuild;
- FX precision/direction;
- settlement conservation;
- refund, overpayment, waiver và fund double-count cases.

### Week 5 — Planning graph

Build:

- itinerary, date/timezone và attendees;
- places, external map links và metadata resolution;
- polls, deadlines, quorum và outcomes;
- bookings, travelers, attachments và reminders;
- two-way links giữa booking/activity/place và expense/planned cost;
- phase-aware Overview.

Validation:

- timezone/date-only cases;
- booking → expense conversion không double-count;
- poll outcome → itinerary;
- offline place-link fallback.

### Week 6 — Coordination, web participation và notifications

Build:

- shared/private packing;
- responsibilities, assignee, due date và linked object;
- lightweight web join/view/contribute theo permission;
- transactional notifications, actionable reminders và digests;
- per-trip mute, quiet hours và privacy-safe previews;
- reusable crew và plan templates.

Validation:

- cross-platform deep links;
- reminder deduplication;
- guest claim/identity merge;
- permission parity giữa mobile và web.

### Week 7 — Offline sync và conflict hardening

Build:

- batch push/pull, server sequence, durable cursors và tombstones;
- retry/backoff, resumable attachment upload và Sync Center;
- entity-specific conflict policy;
- schema-version handshake và stale-client handling;
- device/member access revocation;
- projection reconciliation jobs.

Chaos tests:

- airplane mode và flaky network;
- timeout sau server commit;
- two-device edit/delete conflict;
- app kill giữa local transaction/sync;
- 30 ngày offline;
- stale schema và sai device clock.

### Week 8 — Memories, recap, search và exports

Build:

- selected memories, compression và external album links;
- activity/day/place linking;
- private recap và public-safe share card;
- full-text/filter search;
- CSV, JSON, PDF và accounting-friendly exports;
- plan duplicate/crew reuse;
- data deletion, retention và attachment lifecycle.

Validation:

- recap allowlist/privacy tests;
- large export và signed URL expiry;
- EXIF handling;
- delete/export lifecycle end-to-end.

### Week 9 — Monetization, analytics và cross-module integration

Build:

- Free, Plan Pass và Pro entitlements;
- purchase/restore/refund state;
- onboarding và contextual education;
- analytics events/dashboards với consent;
- support diagnostics và admin tối thiểu;
- cross-module activity feed và notification routing.

Integration scenarios:

- poll → itinerary → booking → expense → budget → settlement → recap;
- fund contribution → fund expense → balance → export;
- guest join web → claim account → offline mobile edits;
- entitlement changes không khóa viewing/settlement/collaboration cơ bản.

### Week 10 — Full regression, security và performance

- automated unit/integration/property/sync/E2E suites;
- iOS/Android/web device matrix;
- IDOR, RLS, invite, upload và log-redaction security tests;
- accessibility, localization và large-text review;
- 10,000-expense stress fixture và low-end Android profiling;
- backup restore rehearsal;
- app-store privacy manifests/disclosures;
- incident, rollback và feature-flag runbooks.

Gate:

- zero known P0/P1;
- zero confirmed data loss;
- all financial invariants green;
- unified screen inventory usable trên supported platforms.

### Week 11 — Real-plan private beta

- 20–50 plans dùng toàn bộ journey;
- daily triage cho crash, sync, calculation, privacy và usability;
- observe organizer/invitee contribution;
- verify third-party/storage cost;
- settlement and recap completion interviews;
- store listing, help center, landing page và support training;
- release candidate freeze ở cuối tuần.

Không bỏ module khỏi scope để xử lý bug. Nếu release gate chưa đạt, dời launch.

### Week 12 — Unified launch candidate và soft launch

- final regression trên signed production builds;
- production migration và rollback rehearsal;
- App Store/Google Play submission;
- web invite deployment;
- ASO/content/creator seeding;
- live dashboards, alerting và on-call coverage;
- staged rollout theo % user;
- go/no-go review dựa trên toàn bộ acceptance criteria Mục 10.8.

### 30.3 Weekly operating rhythm

- Monday: risk/metric review và dependency resolution;
- Daily: automated tests, device smoke và cross-workstream sync;
- Wednesday: beta user review và full-journey demo;
- Thursday: integration branch/release candidate;
- Friday: regression, retro và decision log update;
- Money, authz, sync và privacy contract changes cần review chéo.

### 30.4 Khi lịch trễ

Không cắt feature khỏi unified scope. Chọn một hoặc nhiều biện pháp:

1. dời public launch;
2. tăng nguồn lực cho critical path;
3. giảm visual polish nhưng vẫn giữ accessibility và complete states;
4. dùng managed provider cho non-differentiating infrastructure;
5. đóng băng change request ngoài Mục 10;
6. giảm số thị trường/ngôn ngữ launch, không giảm product capability;
7. thu hẹp beta cohort trong khi tiếp tục hoàn thiện full scope.

Không được đánh đổi ledger correctness, offline durability, authorization, privacy lifecycle, auditability hoặc test coverage để giữ ngày launch.

---

## 31. Definition of success và quyết định còn mở

### 31.1 90-day success hypothesis sau soft launch

- 100+ created plans;
- 50+ activated plans;
- 20+ completed real plans;
- invite open → join ≥50%;
- activated plan rate ≥40% created plans;
- ≥70% activated plans có 2+ contributors;
- ≥40% completed plans settle hoặc confirm no balance;
- crash-free sessions >99.5%;
- no confirmed financial data loss;
- ≥5 organizers chủ động hỏi hoặc trả tiền cho advanced feature.

Các số trên là hypothesis để điều hành, không phải benchmark ngành.

### 31.2 Product decisions cần chốt trước build

1. Guest join dùng anonymous identity có claim hay bắt buộc magic link/passkey?
2. Multiple payers được nhập theo allocation trực tiếp hay theo nguồn thanh toán?
3. Settlement mặc định per-currency hay base-currency preview?
4. Rate source nào, license/cost/cache terms ra sao?
5. Free limits dựa trên active plans, members hay advanced features?
6. Launch language/market đầu tiên là gì?
7. Plan Pass hay subscription là experiment đầu?
8. Lightweight web cho phép read-only, vote/check task hay cả add expense?
9. Privacy policy cho travel/location data và retention cụ thể?
10. Tolerance để coi balance là settled theo từng currency?

### 31.3 Recommended decisions nếu cần bắt đầu ngay

- Magic link/OAuth cho account; guest identity có claim và invite approval.
- Multiple payers là capability bắt buộc của unified financial scope.
- Giữ balances per currency; base-currency là estimate; settlement user chọn và freeze rate.
- Free collaboration, charge organizer cho advanced tools.
- Launch English + Vietnamese nếu team support được localization; nếu không, chọn một ngôn ngữ theo beta cohort.
- Flutter + Postgres/Supabase + SQLite/Drift.
- No real-money movement.

### 31.4 Final product test

Sản phẩm đang đi đúng hướng nếu user có thể nói:

> “Cả nhóm chỉ cần mở Beluno để biết kế hoạch, việc cần làm, budget và ai nợ ai.”

Financial core vẫn là wedge acquisition và trust anchor, nhưng product chỉ đạt definition of done khi toàn bộ planning, coordination, offline, memories và recap cùng hoạt động trên một plan graph như Mục 10.

---

## 32. Nguồn tham khảo

Các nguồn dưới đây dùng để định vị feature landscape tại thời điểm soạn tài liệu; không phải bằng chứng thay thế cho user research:

- [Splitwise — product overview](https://www.splitwise.com/index): balances, group expenses, split methods, settlement, offline/cloud sync và Pro capabilities.
- [Splitwise — multi-currency guidance](https://kb.splitwise.com/balances-and-expenses/how-can-i-manage-a-friendship-or-group-with-multiple-currencies): cách giữ balance theo currency và conversion behavior.
- [Wanderlog — official feature overview](https://wanderlog.com/): itinerary, collaboration, reservations, map, packing, budget và expense splitting.
- [TripIt — Free vs Pro](https://help.tripit.com/en/support/solutions/articles/103000063396-tripit-or-tripit-pro-): itinerary organization, reservation import và travel alerts.
- [TravelSpend — official feature overview](https://travel-spend.com/): offline expense capture, budget, currency conversion, sync/share và split costs.

---

## Phụ lục A — Unified screen inventory

1. Welcome/sign in
2. Plans list
3. Create plan
4. Invite/share
5. Join plan
6. Plan overview
7. Expense feed
8. Add expense — quick
9. Add expense — advanced split
10. Expense detail/history
11. Balances
12. Balance explanation
13. Settlement preview
14. Record settlement
15. Budget overview
16. Category budget edit
17. Members and roles
18. Activity feed
19. Sync status/issues
20. Plan settings/export/archive
21. Notification preferences
22. Profile/account/data controls
23. Itinerary by day
24. Itinerary item detail/editor
25. Shared places list
26. Place detail/votes
27. Poll list, vote và result
28. Bookings list
29. Booking detail/editor
30. Shared packing list
31. Private packing list
32. Responsibilities list/detail
33. Plan fund overview
34. Fund contribution/adjustment
35. Memories timeline
36. Add/select memory
37. Private plan recap
38. Public recap preview/share controls
39. Search and filters
40. Export center
41. Guest claim/identity merge
42. Lightweight web join/view/contribute
43. Purchase/restore entitlement
44. Support diagnostics/report issue

## Phụ lục B — Role/action matrix tối thiểu

| Action | Owner | Admin | Member | Viewer |
|---|:---:|:---:|:---:|:---:|
| View plan/ledger | ✓ | ✓ | ✓ | ✓ |
| Add expense | ✓ | ✓ | ✓ | — |
| Edit own expense | ✓ | ✓ | ✓ | — |
| Edit others’ expense | ✓ | configurable | configurable | — |
| Record settlement involving self | ✓ | ✓ | ✓ | — |
| Manage budget | ✓ | ✓ | configurable | — |
| Invite members | ✓ | ✓ | configurable | — |
| Remove members | ✓ | ✓ except owner | — | — |
| Change roles | ✓ | limited | — | — |
| Export plan | ✓ | ✓ | configurable | configurable |
| Delete plan | ✓ | — | — | — |
| Transfer ownership | ✓ | — | — | — |

## Phụ lục C — Unified analytics events

```text
plan_created
plan_invite_created
plan_invite_opened
plan_joined
expense_local_created
expense_sync_succeeded
expense_sync_failed
expense_edited
expense_voided
split_method_selected
balance_viewed
balance_explanation_viewed
settlement_previewed
settlement_recorded
settlement_reversed
budget_created
budget_threshold_reached
itinerary_item_created
itinerary_item_completed
place_saved
poll_created
poll_vote_submitted
booking_created
booking_linked_to_expense
packing_item_completed
responsibility_completed
fund_contribution_recorded
fund_expense_recorded
memory_added
recap_generated
recap_shared
plan_duplicated
export_requested
purchase_started
purchase_completed
sync_conflict_detected
sync_conflict_resolved
plan_archived
account_export_requested
account_deletion_requested
```

Event properties chỉ dùng ID giả danh, counts/buckets và technical metadata cần thiết; không gửi description, notes, confirmation codes hoặc raw financial content vào analytics.
