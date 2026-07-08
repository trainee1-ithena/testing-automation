# Coverage Summary — create_service_report

**Total cases: 66**


## Common — role-agnostic (25)
**Default URL:** `http://localhost:3000/service_reports`

### field_validation (5)
- [case001] Validate Service Report Type required
- [case002] Validate Service Report Name required
- [case003] Validate Service Report Date required
- [case004] Validate Ticket required
- [case005] Validate Assignee(s) required

### boundary (2)
- [case006] Service Report Number length boundary
- [case007] Service Report Number length exceeds boundary

### field_interaction (4)
- [case008] Ticket selection auto-populates Organization
- [case009] Ticket selection auto-populates Organization User
- [case010] Ticket selection filters Appointment options
- [case011] Ticket selection filters Custom Form options

### ui (5)
- [case012] Verify Create button is visible and interactable [smoke]
- [case013] Verify Service Report Type dropdown is visible and interactable
- [case014] Verify Internal Only toggle is visible and interactable
- [case015] Verify Service Report Date picker is visible and interactable
- [case016] Verify Assignee(s) multi-select is visible and interactable

### industry_best_practice (6)
- [case017] Whitespace-only input in required fields
- [case018] Special characters and SQL injection in text fields
- [case019] Input exceeding maximum length
- [case020] Browser back/forward navigation after submission
- [case021] Re-submission of the same form without refreshing
- [case022] Concurrent form submissions by two users

### post_submission_state (3)
- [case023] Verify SR visibility based on Internal Only toggle
- [case024] Verify SR status after creation
- [case025] Verify SR number generation

## Service Manager (16)

### From the Service Reports List Page (6) — `http://localhost:3000/service_reports`

#### happy_path (1)
- [case026] Create External SR from SR List

#### permission (2)
- [case027] Access Create SR from SR List
- [case028] Direct URL access without authentication

#### business_rule (1)
- [case029] SR creation against non-terminal ticket

#### edge_case (1)
- [case030] Create SR with whitespace SR Name

#### post_submission_state (1)
- [case031] Verify SR post-creation state

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case032] Create External SR from Ticket

#### permission (1)
- [case033] Access Create SR from Ticket

#### business_rule (1)
- [case034] SR creation with auto-attached custom form

#### edge_case (1)
- [case035] Change Ticket after opening Create SR form

#### post_submission_state (1)
- [case036] Verify SR post-creation state from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case037] Create SR alongside Appointment

#### permission (1)
- [case038] Access Create SR from Appointment

#### business_rule (1)
- [case039] SR creation with auto-link to Appointment

#### edge_case (1)
- [case040] Create SR with Internal Only toggle on

#### post_submission_state (1)
- [case041] Verify SR post-creation state from Appointment

## Service Engineer (7)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case042] Create External SR from SR List [smoke]
- [case043] Create Internal SR from SR List

#### permission (2)
- [case044] SE creates SR for cross-department ticket
- [case045] Unauthenticated access to Create SR URL

#### business_rule (1)
- [case046] SR creation against non-terminal ticket

#### edge_case (1)
- [case047] Attempt SR creation with whitespace SR Name

#### post_submission_state (1)
- [case048] Verify post-creation state of SR

## Admin (16)

### From the Service Reports List Page (6) — `http://localhost:3000/service_reports`

#### happy_path (1)
- [case049] Create SR from SR List with all fields [smoke]

#### permission (2)
- [case050] Admin can create SR for any ticket
- [case051] Unauthenticated user cannot access Create SR URL

#### business_rule (1)
- [case052] Admin creates SR with Internal Only toggle

#### edge_case (1)
- [case053] Admin attempts to create SR for Closed ticket

#### post_submission_state (1)
- [case054] Verify post-submission state for Admin-created SR

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case055] Create SR from Ticket with pre-populated fields [smoke]

#### permission (1)
- [case056] Admin can create SR from any ticket's SR tab

#### business_rule (1)
- [case057] Admin creates SR with Internal Only toggle from Ticket

#### edge_case (1)
- [case058] Admin attempts to create SR for Resolved ticket

#### post_submission_state (1)
- [case059] Verify post-submission state for Admin-created SR from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case060] Create SR alongside Appointment for existing ticket [smoke]

#### permission (1)
- [case061] Admin can create SR from Appointment form

#### business_rule (1)
- [case062] Admin creates SR with Internal Only toggle from Appointment

#### edge_case (1)
- [case063] Admin attempts to create SR for Archived ticket from Appointment

#### post_submission_state (1)
- [case064] Verify post-submission state for Admin-created SR from Appointment

## Customer User (2)

### permission_check (2) — `http://localhost:3000/service_reports`

#### permission (2)
- [case065] CU cannot see Create SR option
- [case066] CU cannot access Create SR via direct URL