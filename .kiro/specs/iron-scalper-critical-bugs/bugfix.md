# Bugfix Requirements Document

## Introduction

This document addresses 11 critical bugs in the Iron Scalper trading system that affect security, data integrity, and trading reliability. These bugs must be fixed to bring the system to production-grade quality (9/10+). The bugs span several categories:

- **Security vulnerabilities**: Weak password validation, missing session timeouts, no audit logging
- **Trading reliability**: Time zone fallback bug, magic number collision risk, spread limit logic, micro capital assumptions
- **Data integrity**: Silent error recovery, missing model verification, no database backups
- **Code quality**: Duplicate .gitignore entries

## Bug Analysis

### Current Behavior (Defect)

#### Bug 1: Time Zone Fallback - Trading Reliability

1.1 WHEN the broker time retrieval fails (MT5 connection lost or server timestamp unavailable) THEN the system falls back to local PC time for trading decisions

1.2 WHEN local PC time is used as fallback THEN day boundaries for risk management are calculated using potentially incorrect time zones

1.3 WHEN day boundaries are incorrect THEN daily loss limits may be applied to the wrong trading day

1.4 WHEN day boundaries are incorrect THEN trade count resets may occur at the wrong time

#### Bug 2: Magic Number Collision - Trading Risk

2.1 WHEN the system uses the default magic number `123456` THEN there is high risk of collision with other bots/strategies on the same MT5 account

2.2 WHEN magic number collision occurs THEN the system may incorrectly track positions from other bots as its own

2.3 WHEN magic number collision occurs THEN risk management calculations include positions not managed by this bot

#### Bug 3: Weak Password Validation - Critical Security

3.1 WHEN a user creates a password with only 12 characters and no complexity THEN the system accepts it

3.2 WHEN an attacker attempts multiple login attempts without restriction THEN the system allows unlimited attempts

3.3 WHEN an attacker makes 5+ consecutive failed login attempts THEN the system does not lock the account

3.4 WHEN failed login attempts occur THEN no account lockout or cooldown mechanism is triggered

#### Bug 4: No Model Integrity Verification - Trading Risk

4.1 WHEN the ML model file is loaded from disk THEN no checksum or integrity verification is performed

4.2 WHEN a corrupted or tampered ML model is loaded THEN the system accepts it without validation

4.3 WHEN a corrupted model is used THEN incorrect trading decisions may be made based on bad predictions

#### Bug 5: Silent Error Recovery - Data Integrity

5.1 WHEN database read errors occur in `_restore_day_state()` THEN the system silently defaults to zero values (`realised_usd: 0.0, trades: 0, trailing_losses: 0`)

5.2 WHEN database corruption exists THEN the error is masked by default zero values

5.3 WHEN zero defaults are used incorrectly THEN daily P/L tracking becomes incorrect

5.4 WHEN zero defaults are used incorrectly THEN risk management brakes fail to trigger properly

#### Bug 6: No Database Backup Mechanism - Data Loss Risk

6.1 WHEN the system runs daily trading operations THEN no automated database backups are created

6.2 WHEN database corruption or disk failure occurs THEN all historical trade data is permanently lost

6.3 WHEN data loss occurs THEN performance analysis and audit trails are unrecoverable

#### Bug 7: Missing Session Timeout - Security

7.1 WHEN a user logs into the dashboard and walks away THEN the session persists indefinitely

7.2 WHEN a session has no activity for extended periods (30+ minutes) THEN the user remains logged in

7.3 WHEN unattended sessions remain active THEN unauthorized access risk increases significantly

#### Bug 8: No Audit Logging for Dashboard Actions - Security

8.1 WHEN a user starts or stops the trading engine THEN no audit log entry is created with username and timestamp

8.2 WHEN a user changes critical settings THEN no record of the change or who made it is preserved

8.3 WHEN authentication attempts occur (success or failure) THEN no audit trail is maintained

8.4 WHEN sensitive data is accessed THEN no log entry indicates who accessed it and when

#### Bug 9: Spread Limit Not Strategy-Specific - Logic Bug

9.1 WHEN the global spread limit `max_spread_points: 50.0` is applied to all strategies THEN the scalper strategy accepts spreads up to 50 points

9.2 WHEN scalper trades execute with spreads between 20-50 points THEN entries occur under suboptimal conditions for scalping

9.3 WHEN breakout strategy evaluates spreads THEN it uses the same 50-point limit despite different strategy requirements

#### Bug 10: Duplicate .gitignore Entries - Code Quality

10.1 WHEN developers maintain the `.gitignore` file THEN duplicate entries cause confusion and maintenance overhead

10.2 WHEN duplicate entries exist THEN it's unclear which patterns are intentional vs. accidental

#### Bug 11: System Assumes Standard Account - Untradeable with Micro Capital (CRITICAL)

11.1 WHEN the system uses default `starting_balance: float = 10000.0` THEN it assumes a standard USD account with $10,000 capital

11.2 WHEN the actual account has R50-R100 ZAR (≈ $2.70-$5.40 USD) THEN position sizing calculations are fundamentally incorrect

11.3 WHEN risk calculations use `risk_per_trade: float = 0.01` (1% = $100) THEN a single trade's risk exceeds the entire micro account balance

11.4 WHEN `max_open_positions: int = 3` is configured THEN the system attempts to open 3 positions requiring ~$300 margin but only R100 is available

11.5 WHEN margin requirements for XAUUSD micro lots are checked THEN the system does not validate if the account can support even 1 trade

11.6 WHEN spread and commission costs are calculated THEN they are not expressed as a percentage of the micro account balance

11.7 WHEN the system validates configuration at startup THEN it does not calculate or report the minimum viable account size for the trading mandate

11.8 WHEN configuration violations occur (e.g., 3 concurrent positions impossible with R50) THEN the system does not flag them or refuse to trade

### Expected Behavior (Correct)

#### Bug 1: Time Zone Fallback - Stop Trading on Broker Time Failure

2.1 WHEN the broker time retrieval fails THEN the system SHALL immediately stop all trading operations

2.2 WHEN broker time is unavailable THEN the system SHALL log a CRITICAL error indicating trading cannot continue safely

2.3 WHEN broker time is unavailable THEN the system SHALL NOT use local PC time as a fallback for any trading decisions

2.4 WHEN broker time becomes available again THEN the system SHALL resume trading with correct time-based risk management

#### Bug 2: Magic Number Collision - Generate Unique Magic Number

2.5 WHEN the system is set up for the first time THEN it SHALL generate a unique magic number (8-digit random number)

2.6 WHEN no magic number is explicitly configured THEN the default SHALL be a unique value, not `123456`

2.7 WHEN the magic number is configured THEN it SHALL be validated to ensure it's not a common default value

#### Bug 3: Weak Password Validation - Enforce Strong Security

2.8 WHEN a user creates a password THEN the system SHALL require uppercase, lowercase, number, and special character

2.9 WHEN failed login attempts occur from the same IP THEN the system SHALL enforce rate limiting (max 5 attempts per 15-minute window)

2.10 WHEN 5 consecutive failed login attempts occur for an account THEN the system SHALL lock the account for 15 minutes

2.11 WHEN an account is locked THEN the system SHALL send a notification or log entry indicating the lockout

#### Bug 4: No Model Integrity Verification - Verify Model Checksum

2.12 WHEN the ML model file is loaded THEN the system SHALL verify its checksum against a stored registry value

2.13 WHEN model checksum verification fails THEN the system SHALL refuse to load the model and log a CRITICAL error

2.14 WHEN model checksum verification fails THEN the system SHALL block ML-gated trading (ml.enabled=True should prevent trades)

#### Bug 5: Silent Error Recovery - Distinguish Database Errors from No Data

2.15 WHEN `_restore_day_state()` reads from the database and finds no records for today THEN the system SHALL initialize with zero values (this is correct)

2.16 WHEN `_restore_day_state()` encounters a database read error (corruption, locked file, schema mismatch) THEN the system SHALL log a CRITICAL error and stop trading

2.17 WHEN database errors occur THEN the system SHALL NOT silently default to zero values that mask the problem

#### Bug 6: No Database Backup Mechanism - Automated Daily Backups

2.18 WHEN the system starts trading for a new day THEN it SHALL create a compressed backup of the database before any trades execute

2.19 WHEN daily backups are created THEN the system SHALL rotate backups keeping the most recent 30 days

2.20 WHEN backup creation fails THEN the system SHALL log a WARNING but continue trading (backups are protective, not required for operation)

#### Bug 7: Missing Session Timeout - Implement 30-Minute Inactivity Timeout

2.21 WHEN a dashboard session has no activity for 30 minutes THEN the system SHALL automatically log out the user

2.22 WHEN auto-logout occurs THEN the user SHALL be redirected to the login page

2.23 WHEN the user attempts to interact after session expiration THEN the system SHALL require re-authentication

#### Bug 8: No Audit Logging - Log All Security-Relevant Actions

2.24 WHEN any authentication attempt occurs (success or failure) THEN the system SHALL log username, IP address, timestamp, and outcome

2.25 WHEN the trading engine starts or stops THEN the system SHALL log username, timestamp, and action

2.26 WHEN critical settings change THEN the system SHALL log username, timestamp, setting name, old value, and new value

2.27 WHEN sensitive data is accessed THEN the system SHALL log username, timestamp, and data accessed

#### Bug 9: Spread Limit Strategy-Specific - Per-Strategy Spread Configuration

2.28 WHEN the scalper strategy evaluates spread THEN it SHALL use a strategy-specific limit of 20 points

2.29 WHEN the breakout strategy evaluates spread THEN it SHALL use a strategy-specific limit of 50 points

2.30 WHEN spread configuration is validated THEN each strategy SHALL have its own `max_spread_points` setting

#### Bug 10: Duplicate .gitignore Entries - Clean Deduplicated File

2.31 WHEN the `.gitignore` file is maintained THEN it SHALL contain no duplicate entries

2.32 WHEN duplicate patterns exist THEN they SHALL be consolidated into single entries with comments if needed

#### Bug 11: System Assumes Standard Account - Validate Micro Capital Viability

2.33 WHEN the system validates configuration at startup THEN it SHALL calculate the minimum viable account size in both ZAR and USD required to execute the trading mandate (considering symbol margin requirements, max_open_positions, risk_per_trade)

2.34 WHEN the actual account balance (from MT5) is less than the calculated minimum viable size THEN the system SHALL refuse to trade and log a CRITICAL error stating the required minimum balance

2.35 WHEN position sizing is calculated THEN it SHALL work correctly with ZAR denomination and convert risk amounts to USD for XAUUSD margin calculations

2.36 WHEN risk per trade is calculated THEN it SHALL be reported in BOTH ZAR and USD with clear logging

2.37 WHEN spread and commission costs are calculated THEN they SHALL be expressed as both absolute USD amounts AND as a percentage of the account balance

2.38 WHEN margin requirements for opening N positions are validated THEN the system SHALL check if `account_balance * leverage >= (N * position_margin)` before allowing trades

2.39 WHEN the minimum viable account calculation is logged THEN it SHALL show: broker leverage, symbol margin per micro lot, max simultaneous positions, risk per trade, and minimum required balance in ZAR and USD

2.40 WHEN configuration has `max_open_positions: 3` but account can only support 1 position THEN the system SHALL warn and reduce effective max_open_positions to what the account can support OR refuse to trade

### Unchanged Behavior (Regression Prevention)

#### Bug 1: Time Zone Fallback - Preserve Normal Operation

3.1 WHEN broker time is available and functioning normally THEN the system SHALL CONTINUE TO use broker time for all trading decisions

3.2 WHEN day boundaries are calculated with valid broker time THEN the system SHALL CONTINUE TO apply daily loss limits correctly

3.3 WHEN trade count resets occur with valid broker time THEN the system SHALL CONTINUE TO reset at correct day boundaries

#### Bug 2: Magic Number - Preserve Explicit Configuration

3.4 WHEN a user explicitly sets a magic number in config/settings.py THEN the system SHALL CONTINUE TO use that configured value

3.5 WHEN the configured magic number is validated THEN the system SHALL CONTINUE TO allow any positive integer

#### Bug 3: Password Validation - Preserve User Management

3.6 WHEN users with existing passwords log in THEN the system SHALL CONTINUE TO authenticate them successfully (no forced password resets)

3.7 WHEN password validation is enhanced THEN the system SHALL CONTINUE TO work with the existing authentication flow

3.8 WHEN rate limiting is added THEN legitimate users SHALL CONTINUE TO log in successfully on first attempt

#### Bug 4: Model Integrity - Preserve ML Gate Functionality

3.9 WHEN a valid ML model is loaded with correct checksum THEN the system SHALL CONTINUE TO use it for signal filtering

3.10 WHEN ml.enabled is False THEN the system SHALL CONTINUE TO trade without ML filtering

3.11 WHEN ML predictions are made THEN the system SHALL CONTINUE TO log predictions to the database

#### Bug 5: Database Error Recovery - Preserve Successful Reads

3.12 WHEN database reads succeed and return valid data THEN the system SHALL CONTINUE TO use that data for risk management

3.13 WHEN day state is restored successfully THEN the system SHALL CONTINUE TO apply brakes correctly based on that data

#### Bug 6: Database Operations - Preserve Normal Trading

3.14 WHEN database writes occur during trading THEN the system SHALL CONTINUE TO log trades, signals, and predictions

3.15 WHEN database reads occur during trading THEN the system SHALL CONTINUE TO retrieve positions and day state

3.16 WHEN backups are created THEN the system SHALL CONTINUE TO trade normally without interruption

#### Bug 7: Session Management - Preserve Dashboard Functionality

3.17 WHEN users log in successfully THEN the system SHALL CONTINUE TO create valid sessions

3.18 WHEN active sessions exist within 30-minute window THEN the system SHALL CONTINUE TO allow dashboard access

3.19 WHEN users interact with the dashboard THEN the system SHALL CONTINUE TO refresh session activity timer

#### Bug 8: Audit Logging - Preserve System Operations

3.20 WHEN audit logs are written THEN the system SHALL CONTINUE TO perform all trading operations normally

3.21 WHEN log writes fail THEN the system SHALL CONTINUE TO trade (audit logs are informational, not operational)

#### Bug 9: Spread Limits - Preserve Spread Checking Logic

3.22 WHEN spread is within strategy-specific limits THEN the system SHALL CONTINUE TO allow trade entry

3.23 WHEN spread exceeds strategy-specific limits THEN the system SHALL CONTINUE TO block entry and log the reason

3.24 WHEN spread checks are performed THEN the system SHALL CONTINUE TO use the broker's current bid/ask spread

#### Bug 10: .gitignore - Preserve Git Functionality

3.25 WHEN files matching .gitignore patterns are created THEN Git SHALL CONTINUE TO ignore them

3.26 WHEN developers commit code THEN the system SHALL CONTINUE TO exclude ignored files from version control

3.27 WHEN the .gitignore is cleaned up THEN all currently ignored files SHALL CONTINUE TO be ignored

#### Bug 11: Micro Capital - Preserve Normal Trading with Adequate Capital

3.28 WHEN the account has sufficient balance for the configured trading mandate THEN the system SHALL CONTINUE TO trade normally

3.29 WHEN margin calculations are performed THEN the system SHALL CONTINUE TO use MT5's reported margin requirements for the symbol

3.30 WHEN position sizing is calculated THEN the system SHALL CONTINUE TO respect risk_per_trade percentage limits

3.31 WHEN multiple positions are opened THEN the system SHALL CONTINUE TO enforce max_open_positions limits

3.32 WHEN risk reports are generated THEN the system SHALL CONTINUE TO show realized P/L and daily risk metrics
