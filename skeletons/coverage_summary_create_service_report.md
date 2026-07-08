# Coverage Summary — create_service_report

**Total cases: 73**


## Common — role-agnostic (30)
**Default URL:** `http://localhost:3000/service_reports`

### field_validation (6)
- [case001] Validate Service Report Type is required
- [case002] Validate Internal Only Toggle is required
- [case003] Validate Service Report Name is required
- [case004] Validate Service Report Date is required
- [case005] Validate Ticket is required
- [case006] Validate Assignee(s) is required

### boundary (4)
- [case007] Service Report Name at max length
- [case008] Service Report Name exceeds max length
- [case009] SR number format at max length
- [case010] SR number format exceeds max length

### field_interaction (4)
- [case011] Ticket selection auto-populates Organization
- [case012] Ticket selection auto-populates Organization User
- [case013] Ticket selection filters Appointment options
- [case014] Ticket selection updates Custom Form eligibility

### ui (8)
- [case015] Verify Service Report Type dropdown exists
- [case016] Verify Internal Only toggle exists
- [case017] Verify Service Report Name input exists
- [case018] Verify Service Report Date picker exists
- [case019] Verify Ticket dropdown exists
- [case020] Verify Assignee(s) multi-select exists
- [case021] Verify Appointment dropdown exists
- [case022] Verify Description text area exists

### industry_best_practice (6)
- [case023] Whitespace-only input in required fields
- [case024] Special characters and SQL/script injection
- [case025] Inputs exceeding maximum length
- [case026] Browser back/forward navigation after submission
- [case027] Re-submission of the same form without refreshing
- [case028] Concurrent form submissions by two users

### post_submission_state (2)
- [case029] Verify SR visibility with Internal Only toggle
- [case030] Verify SR visibility with Customer Visibility toggle

## Service Manager (17)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case031] Create External SR from SR List [smoke]
- [case032] Create Internal SR from SR List

#### permission (2)
- [case033] Access Create SR with Permission
- [case034] Direct URL Access Without Authentication

#### business_rule (1)
- [case035] SR Creation Against Non-Terminal Ticket

#### edge_case (1)
- [case036] Attempt SR Creation Against Closed Ticket

#### post_submission_state (1)
- [case037] Verify Post-Creation State of External SR

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case038] Create External SR from Ticket [smoke]

#### permission (1)
- [case039] Access Create SR from Ticket with Permission

#### business_rule (1)
- [case040] SR Creation with Pre-Selected Ticket

#### edge_case (1)
- [case041] Attempt SR Creation with Locked Ticket Fields

#### post_submission_state (1)
- [case042] Verify Post-Creation State of SR from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case043] Create SR from Appointment Form [smoke]

#### permission (1)
- [case044] Access Create SR from Appointment with Permission

#### business_rule (1)
- [case045] SR Creation with Appointment Link

#### edge_case (1)
- [case046] Attempt SR Creation with Invalid Appointment Link

#### post_submission_state (1)
- [case047] Verify Post-Creation State of SR from Appointment

## Service Engineer (7)

### From the Service Reports List Page (7) — `http://localhost:3000/service_reports`

#### happy_path (2)
- [case048] Create External SR from SR List
- [case049] Create Internal SR from SR List

#### permission (2)
- [case050] SE creates SR with cross-department access
- [case051] Unauthenticated user attempts direct URL access

#### business_rule (1)
- [case052] SE creates SR with required fields

#### edge_case (1)
- [case053] SE attempts SR creation on closed ticket

#### post_submission_state (1)
- [case054] Verify SR post-creation state

## Admin (16)

### From the Service Reports List Page (5) — `http://localhost:3000/service_reports`

#### happy_path (1)
- [case055] Create SR from SR List with all fields [smoke]

#### permission (1)
- [case056] Admin can create SR for any ticket

#### business_rule (1)
- [case057] Admin creates SR with required fields

#### edge_case (1)
- [case058] Admin creates SR with no Appointment

#### post_submission_state (1)
- [case059] Verify SR post-creation state

### From Within a Ticket (Service Reports Tab) (5) — `http://localhost:3000/cases`

#### happy_path (1)
- [case060] Create SR from Ticket with pre-populated fields [smoke]

#### permission (1)
- [case061] Admin creates SR for any accessible ticket

#### business_rule (1)
- [case062] Admin creates SR with required fields from Ticket

#### edge_case (1)
- [case063] Admin creates SR with no Appointment from Ticket

#### post_submission_state (1)
- [case064] Verify SR post-creation state from Ticket

### From the Appointment Creation Form (5) — `http://localhost:3000/appointments`

#### happy_path (1)
- [case065] Create SR alongside Appointment [smoke]

#### permission (1)
- [case066] Admin creates SR from Appointment for any ticket

#### business_rule (1)
- [case067] Admin creates SR with required fields from Appointment

#### edge_case (1)
- [case068] Admin creates SR with no linked Appointment

#### post_submission_state (1)
- [case069] Verify SR post-creation state from Appointment

### permission_check (1) — `http://localhost:3000/service_reports`

#### permission (1)
- [case070] Direct URL access without authentication

## Customer User (3)

### permission_check (3) — `http://localhost:3000/service_reports`

#### permission (3)
- [case071] CU cannot see Create SR option
- [case072] CU cannot access Create SR via direct URL
- [case073] Unauthenticated user cannot access Create SR URL