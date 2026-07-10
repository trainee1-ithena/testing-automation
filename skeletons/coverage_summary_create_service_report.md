# Coverage Summary — create_service_report

**Total cases: 54**


## Common — role-agnostic (13)
**Default URL:** `http://localhost:3000/service_reports`

### field_validation (5)
- [case001] Validate Service Report Type is required
- [case002] Validate Service Report Name is required
- [case003] Validate Service Report Date is required
- [case004] Validate Ticket selection is required
- [case005] Validate at least one Assignee is required

### boundary (1)
- [case006] Validate SR number does not exceed 18 characters

### field_interaction (2)
- [case007] Ticket selection auto-populates Organization and User
- [case008] Changing Ticket resets dependent fields

### ui (2)
- [case009] Verify Service Report Type dropdown is visible and interactable
- [case010] Verify Internal Only toggle is visible and interactable

### industry_best_practice (2)
- [case011] Validate whitespace-only input in required fields
- [case012] Test SQL/script injection in text fields

### post_submission_state (1)
- [case013] Verify SR visibility based on Internal Only toggle

## Service Manager (17)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case014] Create External SR from SR List
- [case015] Create Internal SR from SR List

#### permission (2)
- [case016] Service Manager accesses Create SR action
- [case017] Unauthenticated user cannot access Create SR URL

#### business_rule (1)
- [case018] SR creation against non-terminal ticket

#### edge_case (1)
- [case019] Attempt to create SR for Closed ticket

#### post_submission_state (1)
- [case020] Verify post-creation state of External SR

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case021] Create External SR from Ticket

#### permission (1)
- [case022] Service Manager accesses Create SR action from Ticket

#### business_rule (1)
- [case023] SR creation against non-terminal ticket from Ticket

#### edge_case (1)
- [case024] Attempt to create SR for Closed ticket from Ticket

#### post_submission_state (1)
- [case025] Verify post-creation state of External SR from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case026] Create SR alongside Appointment

#### permission (1)
- [case027] Service Manager accesses Create SR action from Appointment

#### business_rule (1)
- [case028] SR creation against non-terminal ticket from Appointment

#### edge_case (1)
- [case029] Attempt to create SR for Closed ticket from Appointment

#### post_submission_state (1)
- [case030] Verify post-creation state of SR from Appointment

## Service Engineer (7)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case031] Create External SR from SR List [smoke]
- [case032] Create Internal SR from SR List

#### permission (2)
- [case033] SE creates SR with cross-department access
- [case034] Unauthenticated access to Create SR URL

#### business_rule (1)
- [case035] SE creates SR with required fields

#### edge_case (1)
- [case036] SE creates SR with no linked appointment

#### post_submission_state (1)
- [case037] Verify SR post-creation state

## Admin (17)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case038] Create SR from SR List with T&M Type [smoke]
- [case039] Create SR from SR List with Fixed Fee Type

#### permission (2)
- [case040] Admin can access Create SR action
- [case041] Unauthenticated user cannot access Create SR URL

#### business_rule (1)
- [case042] Admin creates SR against non-terminal ticket

#### edge_case (1)
- [case043] Admin attempts to create SR for closed ticket

#### post_submission_state (1)
- [case044] Verify SR post-creation state for Admin

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case045] Create SR from Ticket with T&M Type [smoke]

#### permission (1)
- [case046] Admin can access Create SR action from Ticket

#### business_rule (1)
- [case047] Admin creates SR with auto-attached custom form

#### edge_case (1)
- [case048] Admin attempts to create SR with whitespace SR Name

#### post_submission_state (1)
- [case049] Verify SR post-creation state from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case050] Create SR alongside Appointment with T&M Type [smoke]

#### permission (1)
- [case051] Admin can create SR from Appointment form

#### business_rule (1)
- [case052] Admin creates SR with internal visibility from Appointment

#### edge_case (1)
- [case053] Admin creates SR with past date from Appointment

#### post_submission_state (1)
- [case054] Verify SR post-creation state from Appointment