# Coverage Summary — create_service_report

**Total cases: 68**


## Common — role-agnostic (24)
**Default URL:** `http://localhost:3000/service_reports`

### field_validation (5)
- [case001] Validate Service Report Type required
- [case002] Validate Service Report Name required
- [case003] Validate Service Report Date required
- [case004] Validate Ticket required
- [case005] Validate Assignee(s) required

### boundary (2)
- [case006] Service Report Name at max length
- [case007] Service Report Name exceeding max length

### field_interaction (4)
- [case008] Ticket selection auto-populates Organization
- [case009] Ticket selection auto-populates Organization User
- [case010] Ticket selection filters Appointment options
- [case011] Ticket selection updates Custom Form eligibility

### ui (5)
- [case012] Verify Service Report Type dropdown exists
- [case013] Verify Internal Only toggle exists
- [case014] Verify Service Report Date picker exists
- [case015] Verify Assignee(s) multi-select exists
- [case016] Verify Ticket field is locked when pre-selected

### industry_best_practice (6)
- [case017] Whitespace-only input in required fields
- [case018] Special characters and SQL/script injection
- [case019] Inputs exceeding maximum length
- [case020] Browser back/forward navigation after submission
- [case021] Re-submission of the same form without refreshing
- [case022] Concurrent form submissions by two users

### post_submission_state (2)
- [case023] Verify post-creation visibility for Internal Only SR
- [case024] Verify post-creation visibility for Customer Visible SR

## Service Manager (16)

### From the Service Reports List Page (6) — `http://localhost:3000/service_reports`

#### happy_path (1)
- [case025] Create External SR from SR List

#### permission (2)
- [case026] Access Create SR action from SR List
- [case027] Direct URL access without authentication

#### business_rule (1)
- [case028] Create SR against non-terminal ticket

#### edge_case (1)
- [case029] Attempt to create SR against Closed ticket

#### post_submission_state (1)
- [case030] Verify post-creation state of SR

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case031] Create External SR from Ticket

#### permission (1)
- [case032] Access Create SR action from Ticket

#### business_rule (1)
- [case033] Create SR with pre-selected Ticket

#### edge_case (1)
- [case034] Attempt to create SR with whitespace SR Name

#### post_submission_state (1)
- [case035] Verify post-creation state of SR from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case036] Create SR alongside Appointment

#### permission (1)
- [case037] Access Create SR action from Appointment

#### business_rule (1)
- [case038] Create SR with auto-linked Appointment

#### edge_case (1)
- [case039] Create SR with Internal Only toggle On

#### post_submission_state (1)
- [case040] Verify post-creation state of SR from Appointment

## Service Engineer (8)

### From the Service Reports List Page (8) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case041] Create External SR from SR List
- [case042] Create Internal SR from SR List

#### permission (3)
- [case043] SE with permission creates SR
- [case044] SE without permission cannot see Create SR
- [case045] Direct URL access without authentication

#### business_rule (1)
- [case046] SE creates SR with non-terminal ticket

#### edge_case (1)
- [case047] SE tries to create SR with closed ticket

#### post_submission_state (1)
- [case048] Verify SR post-creation state

## Admin (17)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case049] Create SR from SR List with all fields [smoke]
- [case050] Create Internal SR from SR List

#### permission (2)
- [case051] Admin can access Create SR action
- [case052] Unauthenticated user cannot access Create SR URL

#### business_rule (1)
- [case053] Admin creates SR with non-terminal ticket

#### edge_case (1)
- [case054] Admin attempts SR creation with closed ticket

#### post_submission_state (1)
- [case055] Verify SR post-creation state for Admin

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case056] Create SR from Ticket with pre-selected Ticket [smoke]

#### permission (1)
- [case057] Admin can access Create SR action from Ticket

#### business_rule (1)
- [case058] Admin creates SR with locked Ticket field

#### edge_case (1)
- [case059] Admin attempts SR creation with terminal ticket

#### post_submission_state (1)
- [case060] Verify SR post-creation state from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case061] Create SR alongside Appointment [smoke]

#### permission (1)
- [case062] Admin can create SR from Appointment form

#### business_rule (1)
- [case063] Admin creates SR with appointment link

#### edge_case (1)
- [case064] Admin attempts SR creation with linked appointment

#### post_submission_state (1)
- [case065] Verify SR post-creation state from Appointment

## Customer User (3)

### permission_check (3) — `http://localhost:3000/service_reports`

#### permission (3)
- [case066] Verify CU cannot see Create SR option
- [case067] Verify CU cannot access Create SR form via URL
- [case068] Unauthenticated user cannot access Create SR form