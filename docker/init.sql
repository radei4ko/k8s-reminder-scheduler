-- Runs once, when the MySQL container's volume is first created.
-- Sets up a separate database for the test suite so pytest never touches
-- the data a demo run is showing.
CREATE DATABASE IF NOT EXISTS reminder_scheduler_test;
GRANT ALL PRIVILEGES ON reminder_scheduler_test.* TO 'reminder_user'@'%';
FLUSH PRIVILEGES;
