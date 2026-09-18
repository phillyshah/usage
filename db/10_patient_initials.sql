-- 10 — Add patient initials to tickets
-- Holds ONLY the patient's two initials (e.g. "JD") plus the model's confidence
-- in that read, for the "Inits" column of the Usage deliverable. The patient's
-- name, DOB, MRN and every other identifier are never stored — see
-- app/pipeline/patient.py. Populated only when EXTRACT_PATIENT_INITIALS=true;
-- with the flag off these columns simply stay null.
--
-- NOTE: these columns hold PHI. The tickets table is already covered by the
-- retention purge and RLS from the base schema; nothing further is added here.
-- Safe to run more than once.

alter table tickets add column if not exists patient_initials text;
alter table tickets add column if not exists patient_initials_conf text;
