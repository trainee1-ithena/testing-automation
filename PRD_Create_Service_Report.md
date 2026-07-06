# Product Requirements Document: Create Service Report

**Module:** Service Report — Create New  
**Version:** 1.0  
**Scope:** iSERV Web Application (portal)  
**Testing Scope:** Everything that is validated, triggered, or determined at the moment a new Service Report is created — including form fields, field-level validation, business rules, permissions, workflow path selection, custom form attachment, SR number generation, and the resulting post-creation state. Does not cover post-creation actions (worklog logging, status transitions after creation, invoicing, approval, etc.).

**References:**

Workflows and Features/
- Service Report Workflow Document
- Service Report "Closed" + "Internal Only" Feature
- Technical Specification: Service Report Custom Forms by Service Type
- iServ System Design: Billing Profiles, Service Reports, Schedules, and Agent Assignment
- Quotes, Service Reports, Invoices - Pricing and Billing Logic

Updated mds & Help Guides/
- help_guide/
  - create_new_appointment_with_new_service_report.md
  - service_report_number_settings.md
- mobile_v2/
  - manage_service_reports.md (SM)

---

## 1. Overview

A Service Report (SR) is a record that captures work performed against a Ticket. It is the primary unit of service delivery documentation in iSERV. When an SR is created, the system:

- Assigns it an auto-generated SR number
- Sets its initial status to **To Do**
- Records the visibility workflow (Customer Visibility or Internal Only)
- Auto-attaches a custom form (checklist) if one is mapped to the ticket's service type and the selected workflow
- Links it to a Ticket, optionally to an Appointment, and assigns one or more staff members

The SR creation action is available from three entry points in the web application. All entry points result in the same underlying SR creation — they differ only in what is pre-populated when the creation form opens.

---

## 2. Personas and Permissions

### 2.1 Roles Involved in SR Creation

| Role | Description | Can Create SR |
|---|---|---|
| Service Manager (SM) | Manager who oversees service operations. Has department-level access. | ✅ Yes, for tickets in their departments or tickets they participate in |
| Service Engineer (SE) | Field engineer who performs service tasks. | ✅ Yes, for tickets in their departments or tickets they participate in |
| Admin | System administrator with full access. | ✅ Yes |
| Customer User (CU) | End customer who views or interacts with service reports. | ❌ No — Customer Users cannot create Service Reports |

### 2.2 Permission Rules for SR Creation

- A staff user must have the **service report create** permission for the ticket's department to create an SR against that ticket.
- Cross-department collaboration: a staff user who is a participant on a ticket (creator, assignee, accountable, or collaborator) can create SRs on that ticket even if it belongs to a department outside their primary access, provided they have the service report create permission for at least one department.
- If a user lacks create permission, the Create Service Report action is not visible or is disabled.

---

## 3. Creation Entry Points

There are three ways to open the Create Service Report form in the web application. In all cases, the same form is presented and the same creation logic applies. The difference is what is pre-populated.

### Entry Point A: From the Service Reports List Page

- User navigates to the **Service Reports** module (main navigation).
- User clicks the **Create** or **New Service Report** button.
- The form opens with no pre-populated Ticket or Appointment.
- The user must select a Ticket manually, which auto-populates the Organisation and User.

### Entry Point B: From Within a Ticket (Service Reports Tab)

- User opens a Ticket and navigates to its **Service Reports** tab (or sub-section).
- User clicks the **Create** or **+** button to add a new SR.
- The form opens with the **Ticket** field pre-selected and locked to that ticket.
- Organization and Organization User are auto-populated from the ticket and are read-only.

### Entry Point C: From the Appointment Creation Form

- When creating or editing an Appointment against a ticket, the user can optionally choose to create a new SR as part of the same action.
- Within the appointment form, the user selects **Create New Service Report** and fills in the SR-specific sub-fields — labeled **Job Type** and **Visibility** in this form (same fields as Service Report Type §4.1 and Internal Only Toggle §4.2 elsewhere, just relabeled here).
- The SR is created and linked to the appointment when the appointment is saved.
- The appointment form itself requires **Title**, **Description**, **Case** (the ticket), and **Assignee** to be filled by the user — these are not pre-populated. The SR's Name, Date, Assignees, and Description are not separate inputs on this form. Only **Job Type** and **Visibility** are SR-specific fields the user fills directly, after clicking Create New Service Report in Service Report.
- The same business rules (permissions, validation, custom form attachment, SR number generation) apply.

### Entry Point Routes

The following base URLs correspond to each entry point. Tests should navigate to these URLs to reach the correct starting context.

| Entry Point | Base URL |
|---|---|
| Entry Point A — Service Reports List (default) | `http://localhost:3000/service_reports` |
| Entry Point B — From Ticket | `http://localhost:3000/cases` |
| Entry Point C — From Appointment | `http://localhost:3000/appointments` |

The Service Reports List URL (`http://localhost:3000/service_reports`) is the **default** for common tests that are not tied to a specific entry point.

### 3.5 Locked Fields by Entry Point

The table below shows which fields are auto-populated in their specific entry point. Some are defined by entry, and some during filling the form.

| Entry Point | Locked Field Names (exact) | Why locked |
|---|---|---|
| **Entry Point A** — From SR List | `org_id`, `customer_id` | Always auto-populated downstream of Ticket selection; the user selects the Ticket but never directly enters Organization or Organization User |
| **Entry Point B** — From Ticket | `ticket_id`, `org_id`, `customer_id` | Ticket is pre-selected and locked to the source ticket; Organization and Organization User inherit from that locked Ticket |
| **Entry Point C** — From Appointment | `org_id`, `customer_id`, `appointment_id` | Derived once a Case is picked, or inherited once the appointment is saved — never entered directly. case (Case), title (Title), notes (Description), and staff_assigned (Assignee) are required, blank, user-filled fields on the appointment form itself — not locked. The user also selects service_report = "Create New Service Report" to reveal the SR sub-fields, then fills job_type ("Job Type") and report_flow ("Visibility"), which are pre-filled with defaults but remain user-editable. |

---

## 4. Creation Form — Full Field Specification

The following describes every field on the Create Service Report form. Unless noted as "Entry Point C only" or "Entry Point A/B only", the field applies to all entry points.

### 4.1 Service Report Type

| Attribute | Detail |
|---|---|
| Field Label | Service Report Type |
| Field Type | Single-select dropdown or radio buttons |
| Required | ✅ Yes |
| Options | **Time & Material (T&M)** — work is billed based on actual hours logged and materials used. **Fixed Fee (FF/FFM)** — work is billed at a fixed scope price. |
| Default | No default; user must explicitly select |
| Business Rule | The Report Type determines which line item categories are used in the report body (Resources for T&M; Scope Items for FF). It does not affect creation-time behavior otherwise. |
| Validation | Must be one of the two valid options. Cannot be blank. |

### 4.2 Internal Only Toggle (Visibility / report_flow)

| Attribute | Detail |
|---|---|
| Field Label | Internal Only |
| Field Type | Toggle (on/off) |
| Required | ✅ Yes (a value must be set; default is off = Customer Visibility) |
| Default | **Off** — the SR is Customer Visible (External) by default |
| Options | **Off (default):** `report_flow = customer_visibility`. The SR follows the standard approval lifecycle and is visible to the Customer User at specific statuses. **On:** `report_flow = internal_only`. The SR is never visible to the Customer User at any status. |
| UI Feedback | When toggled on, an informational tooltip or label clarifies that the report will not be visible to the customer. |
| Business Rule | This toggle determines which workflow path the SR will follow for its entire lifecycle. It cannot be changed after creation. If Internal Only is toggled on, the "Submit for Approval" step is permanently bypassed and the SR follows the Internal workflow (see Section 7). |
| Validation | No format validation; toggle must be in one of its two states. |

### 4.3 Custom Form (Checklist)

| Attribute | Detail |
|---|---|
| Field Label | Checklist / Custom Form |
| Field Type | Auto-populated display or single-select dropdown (if multiple forms are eligible) |
| Required | ❌ No — only shown if a custom form is configured for the ticket's service type and selected report_flow |
| Default | If exactly one form is eligible, it is auto-selected. If multiple forms are eligible, the default form (isDefault = true) is pre-selected but can be changed. |
| Business Rule | The form is resolved at SR creation time based on: (1) the Ticket's Service Type, and (2) the selected report_flow (Internal or External). If a custom form is mapped for that service type + flow combination, it is attached. The form is **snapshotted** at creation — future edits to the form template do not affect this SR. If the Ticket's service type is later changed, the attached form on this SR is not retroactively updated. |
| Validation | If shown, only forms eligible for the service type + flow combination are selectable. |
| Edge Cases | If the selected Ticket has no service type, or the service type has no mapped form for the selected flow, this field does not appear. |

### 4.4 Service Report Name

| Attribute | Detail |
|---|---|
| Field Label | Service Report Name |
| Field Type | Text input |
| Required | ✅ Yes |
| Max Length | Not explicitly constrained in requirements; standard text field |
| Default | None |
| Validation | Cannot be blank. |

### 4.5 Service Report Date

| Attribute | Detail |
|---|---|
| Field Label | Service Report Date |
| Field Type | Date picker |
| Required | ✅ Yes |
| Default | Current date (today) at time of form opening |
| Validation | Must be a valid date. Field cannot be blank. |
| Notes | This is the report date, not a system timestamp. It is editable by the user. |

### 4.6 Ticket

| Attribute | Detail |
|---|---|
| Field Label | Ticket |
| Field Type | Searchable single-select dropdown |
| Required | ✅ Yes |
| Pre-populated | Yes, when opened from within a Ticket (Entry Point B) or from an Appointment (Entry Point C). In those cases, the field is locked and cannot be changed. |
| Filter Logic | Only tickets where the user has service report create permission are shown. Terminal tickets (Closed, Resolved, Archived, Deleted) are excluded — an SR cannot be created against a terminal ticket. |
| Downstream Effects | When a Ticket is selected, ALL of the following occur simultaneously: (1) **Organization** — auto-populated from the ticket's linked organisation; becomes read-only. (2) **Organization User** — auto-populated from the ticket's linked customer user; becomes read-only. (3) **Appointment** — options filtered to show only appointments belonging to this ticket that do not already have a linked SR. (4) **Custom Form** — eligibility re-evaluated based on the ticket's service type and the selected report_flow. Each of these is a distinct downstream effect and must be tested individually. |
| Business Rule | An SR can only be created against a non-terminal ticket. Selecting a different ticket resets all four downstream fields to reflect the new ticket. |
| Validation | Must be selected. Terminal tickets are not selectable. |

### 4.7 Organization (Auto-populated)

| Attribute | Detail |
|---|---|
| Field Label | Organization |
| Field Type | Read-only display field |
| Required | N/A (auto-populated) |
| Source | Populated automatically from the selected Ticket's linked organization (customer). |
| Editable | ❌ No — cannot be manually changed. Changes by selecting a different Ticket. |

### 4.8 Organization User (Auto-populated)

| Attribute | Detail |
|---|---|
| Field Label | Organization User |
| Field Type | Read-only display field |
| Required | N/A (auto-populated) |
| Source | Populated automatically from the selected Ticket's linked customer user. |
| Editable | ❌ No — cannot be manually changed. Changes by selecting a different Ticket. |

### 4.9 Assignee(s)

| Attribute | Detail |
|---|---|
| Field Label | Assignee(s) |
| Field Type | Multi-select staff picker |
| Required | ✅ Yes — at least one assignee must be selected |
| Options | Active staff members who can be assigned to SRs. Options may be filtered to the ticket's department and the user's access scope. |
| Business Rule | Assignees are the staff members who collaborate on the SR. They are stored as `service_report_collaborators`. Selecting assignees also appends those staff members to the ticket's collaborators if not already present. |
| Validation | At least one assignee must be selected. |

### 4.10 Appointment (Optional Link)

| Attribute | Detail |
|---|---|
| Field Label | Appointment |
| Field Type | Single-select dropdown |
| Required | ❌ No — optional |
| Filter Logic | Only Appointments belonging to the selected Ticket that do **not** already have a linked Service Report are shown. An appointment can only be linked to one SR at a time. |
| Business Rule | Linking an Appointment connects the SR to the appointment record. When the SR status changes, the linked appointment's status may be updated in turn (e.g., when SR moves to Submitted, appointment can move to Awaiting Approval). This linkage also gates certain appointment-level actions. |
| Validation | If selected, must be a valid appointment on the selected ticket that is not already linked to another SR. |
| Entry Point C Note | When creating an SR from within the Appointment creation form, the SR is automatically linked to that appointment. No manual selection is needed. |

### 4.11 Description

| Attribute | Detail |
|---|---|
| Field Label | Description |
| Field Type | Multi-line text area |
| Required | ❌ No — optional |
| Default | Empty |
| Validation | No minimum length required. |

---

## 5. Form Submission and Validation

### 5.1 Required Field Summary

| Field | Required |
|---|---|
| Service Report Type | ✅ Yes |
| Internal Only Toggle | ✅ Yes (default = off) |
| Custom Form | Conditional (if eligible form exists, shown; selection required only if form is present) |
| Service Report Name | ✅ Yes |
| Service Report Date | ✅ Yes |
| Ticket | ✅ Yes |
| Organization | Auto-populated, not user-entered |
| Organization User | Auto-populated, not user-entered |
| Assignee(s) | ✅ Yes (at least one) |
| Appointment | ❌ Optional |
| Description | ❌ Optional |

### 5.2 Submission Behaviour

- On successful submission: the SR is created, the system displays a success confirmation, and the user is navigated to the SR detail view or returned to the previous screen (depending on entry point).
- On validation failure: the form remains open and error messages are displayed per field. Required fields that are blank are highlighted.
- The SR is not created until all required fields pass validation and the form is submitted.

---

## 6. SR Number Generation

Every SR is assigned a unique SR number automatically at the time of creation. The number format is configurable by administrators in **System Settings → Service Reports Settings**.

### 6.1 Format Components

| Component | Description | Required |
|---|---|---|
| Prefix | Fixed text at the start of the number (e.g., "SR") | Optional |
| Date Segment | A date in a configurable format inserted after the prefix (e.g., "YYMMDD") | Optional; only included if **Include Date** is enabled |
| Sequence | An auto-incrementing integer with configurable starting value and increment step | ✅ Required |
| Suffix | Fixed text at the end of the number | Optional |

### 6.2 Format Rules and Validation

- The full SR number (prefix + date + sequence + suffix) must not exceed **18 characters**.
- The **Sequence** and **Sequence Increment** values must each be positive whole numbers with a minimum value of 1.
- If **Include Date** is enabled, the **Date Format** field is required; the system blocks SR creation (and settings save) when the date format is blank.
- Administrators can preview the resulting number format before saving settings.
- SR numbers are unique — the sequence auto-increments on every new SR creation.
- SR numbers are system-assigned and not editable by users.

---

## 7. Workflow Path — Determined at Creation

The **Internal Only** toggle at creation determines the SR's lifecycle path permanently. This path cannot be changed after the SR is created.

### 7.1 Customer Visibility Flow (Internal Only = Off, default)

- `report_flow = customer_visibility`
- The SR is visible to the Customer User (CU) once it reaches **Submitted** status.
- Full status lifecycle: **To Do → In Progress → Submitted → Approved → Invoiced → Closed**
- The CU can approve or reject the report when it is in Submitted status.
- The SM can override CU's approval or rejection decision.
- A visibility chip labeled **(External)** is displayed on the SR card and detail view.

### 7.2 Internal Only Flow (Internal Only = On)

- `report_flow = internal_only`
- The SR is **never visible to the Customer User at any status**.
- The "Submit for Approval" action is permanently hidden for this SR.
- Simplified status lifecycle: **To Do → In Progress → Completed → Closed**
- The SR goes directly from In Progress to Completed without a customer approval step.
- A visibility chip labeled **(Internal)** is displayed on the SR card and detail view.
- All notifications related to this SR are stripped of customer recipients — no emails or in-app alerts are ever sent to Customer Users for internal SRs.

### 7.3 Initial Status at Creation

Regardless of which workflow path is selected, every newly created SR starts with status **To Do**.

---

## 8. Custom Form Auto-Attachment Logic

Custom forms (checklists) can be pre-configured by administrators to automatically attach to SRs when certain conditions are met at creation time.

### 8.1 Resolution Logic

When an SR is being created, the system performs the following lookup:

1. Identify the **Ticket's Service Type**.
2. Identify the **report_flow** selected on the creation form (customer_visibility or internal_only).
3. Look up any custom forms in the `service_report_form_service_types` table that match both the Service Type and the report_flow.
4. If exactly one matching form is found, it is auto-selected.
5. If multiple matching forms are found, the form with `isDefault = true` is pre-selected; the user may change it.
6. If no matching form is found, no custom form is attached and the field does not appear.

### 8.2 Snapshotting

When a custom form is attached at SR creation:
- A **snapshot** of the current form template is stored against the SR.
- Future edits to the form template do not affect this SR's attached form.
- Changing the Ticket's Service Type after the SR has been created does **not** retroactively change the attached form on the SR.

### 8.3 Form Visibility Configuration

Forms are configured with a **Report Flow** scope during admin setup:
- **External / Customer Visible**: only auto-attaches to SRs with `report_flow = customer_visibility`.
- **Internal**: only auto-attaches to SRs with `report_flow = internal_only`.
- **Both**: eligible for either flow type.

---

## 9. Post-Creation State

After successful SR creation, the following state is established:

| Attribute | Value |
|---|---|
| `report_status` | `to_do` |
| `report_flow` | `customer_visibility` (default) or `internal_only` (if toggled) |
| `report_type` | `time_and_material` or `fixed_fee` per user selection |
| SR Number | Auto-generated per configured format |
| Linked Ticket | The selected ticket |
| Organization | Inherited from ticket |
| Organization User | Inherited from ticket |
| Assignees | The selected staff members (stored as `service_report_collaborators`) |
| Linked Appointment | The selected appointment, if any (or the appointment created alongside this SR in Entry Point C) |
| Custom Form | Snapshotted form attached if a matching form was found; otherwise none |
| `report_create` notification | Triggered — sent to relevant staff recipients (never to CU for internal SRs) |
| Visibility chip | (Internal) chip for internal SRs; (External) chip for customer-visible SRs |
| Deletability | An SR in To Do status with no worklogs and no linked quote **can be deleted** by users with delete permission. |

---

## 10. Creation From Appointment Flow — Additional Detail

When creating an SR as part of the Appointment creation form (Entry Point C):

### 10.1 Within an Existing Ticket Appointment Form

Steps to create an SR alongside an appointment:
1. User opens the Appointment creation form for an existing ticket.
2. In the Appointment form, user checks or selects **Create New Service Report**.
3. User selects:
   - **Job Type / SR Type**: Time & Material or Fixed Fee (required).
   - **Visibility**: External (Customer Visible) or Internal Only (required).
4. User submits the appointment form.
5. The system creates both the appointment and the linked SR simultaneously.
6. The SR is linked to the appointment automatically.

### 10.2 Within a New Ticket Appointment Form (Multi-Step)

When creating both a new ticket and a new appointment together:
1. Step 1: User enters appointment details.
2. Step 2: User enters new ticket details (Customer, Customer User, Equipment, Customer Location, Service Type, Department, Accountable, Assignee, Priority, Due Date).
3. Within Step 2, the user may optionally select a **Service Report Pack** or individual templates, or choose **Create New Service Report** and select the SR Type and Visibility.
4. Step 3 (conditional): A checklist step appears if a custom form is configured for the selected service type and equipment.
5. On submit, the system creates: the ticket, the appointment, and the SR (linked to the appointment).

### 10.3 Restrictions in Appointment Flow SR Creation

- Creating a new SR from the appointment form requires the same SR create permission as the standalone flow.
- Only the SR Type and Visibility are user-selected in this flow; Name, Date, Assignees, and Description are either derived from the appointment context or use defaults.
- The appointment must be against a non-terminal ticket.

---

## 11. Business Rules Summary

The following rules govern the entire Create SR flow and must be tested:

| # | Rule |
|---|---|
| BR-01 | Customer Users cannot create Service Reports under any circumstances. |
| BR-02 | An SR can only be created against a non-terminal ticket. Terminal statuses (Closed, Resolved, Archived, Deleted) prevent SR creation. |
| BR-03 | Service Report Type (T&M or Fixed Fee) is required. There is no default — the user must explicitly choose. |
| BR-04 | The Internal Only toggle defaults to Off (Customer Visibility). The selection is permanent — it cannot be changed after creation. |
| BR-05 | Service Report Name is required and cannot be blank. |
| BR-06 | Service Report Date is required and defaults to today's date. |
| BR-07 | At least one Assignee must be selected. |
| BR-08 | Organization and Organization User are auto-populated from the selected Ticket and are read-only. They cannot be manually overridden. |
| BR-09 | The Appointment field only shows appointments belonging to the selected Ticket that do not already have a linked SR. An appointment cannot be linked to more than one SR. |
| BR-10 | The SR starts in To Do status regardless of which workflow path was selected at creation. |
| BR-11 | Custom forms (checklists) are auto-attached based on the ticket's service type + selected report_flow. The form is snapshotted at creation. |
| BR-12 | The SR number is auto-generated at creation and cannot be edited by users. |
| BR-13 | The SR number format respects admin-configured settings: prefix, optional date segment, sequence, suffix. The total cannot exceed 18 characters. |
| BR-14 | For Internal Only SRs, the notification system strips all customer recipients. No emails or in-app alerts are ever sent to CUs for internal SRs. |
| BR-15 | A `report_create` notification event is triggered on successful SR creation. |
| BR-16 | Selecting a Ticket also filters the custom form options to those mapped to that ticket's service type and the selected flow. |
| BR-17 | Selecting Assignees also appends those staff members to the linked Ticket's collaborator list if they are not already present. |
| BR-18 | An SR in To Do status with no work logs and no linked quote can be deleted by users with delete permission. This is the only status from which deletion is permitted. |
| BR-19 | When creating an SR from an appointment, the SR is automatically and permanently linked to that appointment. |
| BR-20 | If the Appointment field is left empty, the SR is not linked to any appointment and can be linked to one later (before it is submitted). |

---

## 12. Acceptance Criteria by Area

### 12.1 Permission and Access Control

- ✅ SM and SE with SR create permission can access and successfully submit the Create SR form.
- ✅ A staff user without SR create permission cannot see or access the Create SR action.
- ✅ A Customer User has no Create SR option visible anywhere in the application.
- ✅ A staff user with cross-department collaboration access (ticket participant) can create an SR on a ticket outside their primary department, provided they have the create permission for at least one department.
- ✅ Admin can create SRs for any ticket they have access to.

### 12.2 Form Entry Points

- ✅ Creating from the SR List page opens the form with no pre-selections; the Ticket field is editable.
- ✅ Creating from within a Ticket pre-populates and locks the Ticket field; Organization and Organization User are auto-populated.
- ✅ Creating from the Appointment form (existing ticket flow) allows SR Type and Visibility selection; the SR is created alongside the appointment on submit.
- ✅ Creating from the Appointment form (new ticket flow) creates the ticket, appointment, and SR together in a single submit action.

### 12.3 Required Field Validation

- ✅ Submitting the form without selecting SR Type shows a validation error.
- ✅ Submitting the form without entering SR Name shows a validation error.
- ✅ Submitting the form without selecting SR Date shows a validation error.
- ✅ Submitting the form without selecting a Ticket (when not pre-selected) shows a validation error.
- ✅ Submitting the form without selecting at least one Assignee shows a validation error.
- ✅ Submitting the form with all required fields completed results in successful SR creation.

### 12.4 Visibility Workflow (Internal Only Toggle)

- ✅ By default (toggle off), a created SR has `report_flow = customer_visibility` and displays an (External) chip.
- ✅ With the toggle on, a created SR has `report_flow = internal_only` and displays an (Internal) chip.
- ✅ An internal SR is not visible to the Customer User in the SR list, from the ticket's SR tab, or via direct URL.
- ✅ The Internal Only toggle value is persisted after creation and cannot be changed.
- ✅ For an internal SR, the "Submit for Approval" action is never shown at any point in its lifecycle.

### 12.5 Custom Form Auto-Attachment

- ✅ When a matching form exists for the ticket's service type + external flow, it auto-attaches on creation of an external SR.
- ✅ When a matching form exists for the ticket's service type + internal flow, it auto-attaches on creation of an internal SR.
- ✅ When no matching form exists, the custom form field does not appear and no form is attached.
- ✅ When multiple eligible forms exist, the default form (isDefault = true) is pre-selected and the user can change it.
- ✅ The attached form is a snapshot — verifiable by editing the form template after creation and confirming the SR's form is unchanged.

### 12.6 SR Number Generation

- ✅ A new SR number is assigned on creation and is not editable.
- ✅ The SR number format matches the admin-configured prefix, date, sequence, and suffix.
- ✅ Successive SR creations produce unique, incrementing SR numbers.
- ✅ The SR number does not exceed 18 characters under any configured format.

### 12.7 Appointment Linking

- ✅ Selecting an Appointment links the SR to it; the appointment is reflected on the SR detail and vice versa.
- ✅ An appointment already linked to another SR does not appear in the Appointment dropdown.
- ✅ Leaving Appointment empty creates the SR with no linked appointment.
- ✅ When creating an SR via the Appointment form, the SR is automatically linked to that appointment.

### 12.8 Post-Creation State Verification

- ✅ Newly created SR is in **To Do** status.
- ✅ SR is listed in the SR list and visible to SM and SE with access to that department.
- ✅ SR is not visible to the CU at To Do status (regardless of flow).
- ✅ SR is not visible to the CU at any status if `report_flow = internal_only`.
- ✅ A `report_create` notification is triggered to the appropriate staff recipients.
- ✅ For internal SRs, no customer notifications are triggered.
- ✅ An SR in To Do status with no work logs and no linked quote can be deleted by a user with delete permission.
- ✅ Assignees selected at creation appear on the SR's assignee list and are added as ticket collaborators if not already present.

### 12.9 Terminal Ticket Guard

- ✅ A Closed ticket does not appear in the Ticket dropdown when creating an SR.
- ✅ A Resolved ticket does not appear in the Ticket dropdown.
- ✅ An Archived or Deleted ticket does not appear in the Ticket dropdown.

---

## 13. Persona Journeys

### 13.1 Service Manager Creating an External SR from a Ticket

1. SM opens a ticket in In Progress status.
2. SM navigates to the Service Reports tab within the ticket.
3. SM clicks Create New Service Report.
4. SM selects SR Type: Time & Material.
5. SM leaves Internal Only toggle off (default).
6. A custom form auto-attaches based on the ticket's service type (if configured).
7. SM enters SR Name and confirms today's date.
8. SM selects one or more Assignees.
9. SM optionally selects a linked Appointment from the available list.
10. SM clicks Save.
11. SR is created in To Do status, assigned the next SR number, with (External) chip visible.

### 13.2 Service Engineer Creating an Internal SR from the SR List

1. SE navigates to the Service Reports module.
2. SE clicks Create.
3. SE selects SR Type: Fixed Fee.
4. SE toggles Internal Only ON.
5. SE enters SR Name and Date.
6. SE searches for and selects a Ticket.
7. Organization and Organization User auto-populate.
8. SE selects themselves as Assignee.
9. No Appointment linked.
10. SE clicks Save.
11. SR is created in To Do status with (Internal) chip. CU cannot see this SR.

### 13.3 Service Manager Creating an SR Alongside an Appointment (Existing Ticket)

1. SM opens the Appointments module and clicks New Appointment.
2. SM selects an existing non-terminal Ticket.
3. SM fills in appointment Title, Description, Start/End DateTime, and Time Zone.
4. SM selects Customer Location and Assignee for the appointment.
5. SM checks Create New Service Report.
6. SM selects SR Type: Time & Material and Visibility: External.
7. SM clicks Submit.
8. Both the appointment and the SR are created. The SR is linked to the appointment in To Do status.

### 13.4 Service Manager Creating an SR Alongside an Appointment (New Ticket)

1. SM opens the Appointments module and clicks New Appointment.
2. SM fills in appointment details (Step 1).
3. SM selects Create New Ticket and fills in ticket details — Customer, Customer User, Equipment, Customer Location, Service Type, Department, Accountable, Assignee, Priority, Due Date (Step 2).
4. SM selects Create New Service Report, chooses SR Type and Visibility.
5. If a checklist is configured for the service type and equipment, a Step 3 appears for the checklist.
6. SM submits.
7. A new Ticket, a new Appointment, and a new SR are all created in a single operation. SR is in To Do status and linked to the appointment.

---

## 14. Edge Cases and Negative Scenarios

| # | Scenario | Expected Behaviour |
|---|---|---|
| EC-01 | User tries to create an SR against a Closed ticket | Closed tickets do not appear in the Ticket dropdown; creation is blocked |
| EC-02 | User submits with SR Name as whitespace only | System should treat blank or whitespace-only as invalid and show error |
| EC-03 | User tries to link an Appointment that already has an SR | That appointment does not appear in the Appointment dropdown |
| EC-04 | No custom form is configured for the ticket's service type | Custom form field does not appear; SR is created without a form |
| EC-05 | Two staff create SRs simultaneously — duplicate SR numbers | System must assign unique incremented SR numbers to each |
| EC-06 | Ticket has no Organization User linked | Organization User field shows empty/none; SR can still be created |
| EC-07 | User opens Create SR form and then changes SR Type | Custom form eligibility should re-evaluate if form scope differs by type (if applicable per configuration) |
| EC-08 | User opens Create SR form, selects a Ticket, then selects a different Ticket | Organization and Organization User update to reflect the new ticket; available Appointments refresh |
| EC-09 | User creates an SR without selecting an Appointment | SR is created with no appointment link; this is valid |
| EC-10 | Customer User navigates directly to the Create SR URL | Receives a "Not Authorized" error or is redirected; no SR is created |
| EC-11 | SR number configuration results in format exceeding 18 characters | Admin settings form blocks saving that configuration; SR numbers stay within 18 characters |
| EC-12 | SR Date is set to a past date | The field should accept past dates (it is a report date, not restricted to future) |
| EC-13 | Internal Only is toggled On for an SR linked to an appointment | SR is created as internal; appointment is linked normally; CU cannot see the SR at any point |
| EC-14 | User creates SR with multiple assignees | All selected assignees appear on the SR; all are added to ticket collaborators |
| EC-15 | Custom form has required fields | The form fields are shown in the SR detail after creation; they are not validated at creation time — only at review/submission time |

---

## 15. Data Model Reference (Creation-Relevant Fields)

The following fields are set on the `service_report_header` record at creation time:

| DB Field | Set From |
|---|---|
| `report_status` | System-set to `to_do` |
| `report_flow` | `customer_visibility` (default) or `internal_only` (from Internal Only toggle) |
| `report_type` | User selection (T&M or Fixed Fee) |
| `report_number` | Auto-generated per SR number settings |
| `report_name` | User-entered SR Name |
| `report_date` | User-entered SR Date |
| `ticket_id` | Selected Ticket |
| `org_id` / `customer_id` | Inherited from Ticket |
| `appointment_id` | Selected Appointment (nullable) |
| `custom_form_snapshot` | Snapshotted form (if resolved) |
| `created_by_staff_id` | Logged-in user |
| `created_at` | System timestamp |

Assignees are stored separately in `service_report_collaborators` (one row per assignee, linked to the SR header).

---

*End of PRD — Create Service Report Module*
