-- H5: credencial no adivinable para gestionar turnos públicos.
ALTER TABLE appointments ADD COLUMN management_token_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_appointments_management_token
ON appointments (management_token_hash);
