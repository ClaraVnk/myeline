-- MariaDB initialization script for veille bibliographique
-- This script runs when the container starts for the first time

-- Set character set and collation
ALTER DATABASE myeline CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Create additional indexes if needed for performance
-- These will be created by Flask-Migrate but can be added here for initial setup

-- Set timezone
SET time_zone = '+00:00';

-- Grant permissions (already done by environment variables, but ensuring)
GRANT ALL PRIVILEGES ON myeline.* TO 'myeline_user'@'%';
FLUSH PRIVILEGES;
