# Iron Scalper Critical Bugs - Bugfix Design

## Overview

This design addresses 11 critical bugs preventing the Iron Scalper from being production-ready. The bugs span security vulnerabilities, trading reliability issues, data integrity problems, and capital assumption failures. The fixes ensure the system can:

1. **Trade safely** with proper time zone handling, unique magic numbers, and strategy-specific spread limits
2. **Secure the dashboard** with strong passwords, session timeouts, and comprehensive audit logging
3. **Protect data integrity** with model verification, explicit error handling, and automated backups
4. **Support micro capital** by validating account viability and refusing to trade when undercapitalized

The design follows the bug condition methodology: for each bug, we formalize C(X) (the condition that triggers the bug), define P(result) (the expected correct behavior), and specify preservation requirements (what must remain unchanged).

## Glossary

- **Bug_Condition (C)**: The condition that triggers each specific bug
- **Property (P)**: The desired correct behavior when the bug condition occurs
- **Preservation**: Existing functionality that must remain unchanged by the fix
- **Broker Time**: UTC timestamp from MT5 server, the authoritative clock for trading decisions
- **Magic Number**: Integer identifier that marks this bot's orders in MT5 to distinguish them from other bots
- **Day Boundary**: Midnight in broker time zone, when daily risk limits reset
- **Session Timeout**: Automatic logout mechanism after inactivity period
- **Audit Log**: Tamper-evident record of security-relevant actions with username, IP, timestamp
- **Model Checksum**: SHA-256 hash verifying ML model file integrity against corruption/tampering
- **Spread**: Bid-ask difference in points (price × 100), cost of entering a position
- **Strategy-Specific Limit**: Per-strategy configuration value (not global) for parameters like max_spread
- **ZAR**: South African Rand, the account's native currency
- **Micro Account**: Broker account with R50-R100 capital (~$2.70-$5.40 USD) on micro/cent broker
- **Volume Min**: Minimum tradeable lot size for a symbol on the broker
- **Margin Requirement**: Capital needed to open one position, calculated as (volume × contract_size × price) / leverage
- **Minimum Viable Account**: Smallest balance that can execute the trading mandate (considering margin for max_open_positions, risk_per_trade, spread costs)
- **get_broker_time()**: Function in trading engine that retrieves MT5 server timestamp
- **_restore_day_state()**: Function in risk manager that loads daily P/L and trade count from database
- **isBugCondition()**: Formal predicate defining when each bug manifests

## Bug Details

### Bug 1: Time Zone Fallback

The bug manifests when MT5 connection is lost or broker server timestamp becomes unavailable. The `get_broker_time()` function falls back to local PC time (`datetime.now()`) instead of stopping trading. This causes day boundaries to be calculated incorrectly, applying daily loss limits and trade count resets at wrong times.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug1(state)
  INPUT: state of type TradingEngineState
  OUTPUT: boolean
  
  RETURN (state.mt5_connected = FALSE OR state.broker_timestamp = NULL)
         AND state.using_fallback_time = TRUE
         AND state.trading_active = TRUE
END FUNCTION
```

**Examples:**
- MT5 terminal crashes during trading → system uses PC time → day boundary at wrong time → daily loss limit resets incorrectly → overtrades by $300
- Network interruption at 23:55 broker time → PC time 23:55 local (different timezone) → Asian session boundaries wrong → breakout strategy trades outside intended window
- Broker server timestamp API fails → system continues with PC time → cannot reliably determine session boundaries
- PC clock set to PST when broker is UTC → 8-hour offset → all time-based logic operates on incorrect schedule

### Bug 2: Magic Number Collision

The bug manifests when the system uses hardcoded default `magic=123456` to identify orders. This creates collision risk with other bots/strategies on the same MT5 account. The system may track other bots' positions as its own, corrupting risk management calculations.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug2(config)
  INPUT: config of type MT5Config
  OUTPUT: boolean
  
  RETURN config.magic IN {123456, 111111, 999999, 100000, 123123}
         OR (config.magic = default_value AND NOT explicitly_set_by_user)
END FUNCTION
```

**Examples:**
- Iron Scalper magic=123456, manual test order magic=123456 → system tracks manual order → attempts to manage it → incorrectly closes test order
- Two strategy instances (scalper + breakout) both use magic=123456 → cannot distinguish which opened which → statistics meaningless
- Backtest uses magic=123456, then live bot with same magic → cannot separate historical vs. live orders in MT5 history

### Bug 3: Weak Password Validation

The bug manifests when dashboard authentication accepts weak passwords and allows unlimited login attempts. No password complexity requirements, no rate limiting on failed attempts, no account lockout mechanism.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug3(auth_event)
  INPUT: auth_event of type AuthenticationEvent
  OUTPUT: boolean
  
  RETURN (auth_event.type = "password_creation" 
          AND NOT has_complexity(auth_event.password))
         OR (auth_event.type = "login_attempt"
             AND auth_event.failed_attempts > 5
             AND auth_event.account_locked = FALSE)
         OR (auth_event.attempts_in_window(15_minutes) > 5
             AND auth_event.rate_limited = FALSE)
END FUNCTION

FUNCTION has_complexity(password)
  RETURN len(password) >= 12
         AND has_uppercase(password)
         AND has_lowercase(password)
         AND has_digit(password)
         AND has_special_char(password)
END FUNCTION
```

**Examples:**
- User creates password "password123" (12 chars, no uppercase/special) → accepted → brute forced in minutes
- Attacker makes 100 login attempts in 1 minute → all allowed → credential stuffing succeeds
- 5 failed attempts → account not locked → attacker continues forever

### Bug 4: No Model Integrity Verification

The bug manifests when ML model file (`models/signal_filter.json`) is loaded without checksum verification. Corrupted or tampered model produces incorrect predictions, leading to bad trading decisions.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug4(ml_state)
  INPUT: ml_state of type MLEngineState
  OUTPUT: boolean
  
  RETURN ml_state.ml_enabled = TRUE
         AND ml_state.model_loaded = TRUE
         AND ml_state.checksum_verified = FALSE
END FUNCTION
```

**Examples:**
- Disk corruption → model file contains garbage → predictions random → trades against good signals
- Attacker modifies model weights → predictions inverted → good signals rejected, bad signals accepted
- Model from different version → feature mismatch → crashes or bad predictions

### Bug 5: Silent Error Recovery

The bug manifests when `_restore_day_state()` encounters database read error (corruption, locked file, schema mismatch) but silently defaults to zeros. This masks the error and causes incorrect daily P/L tracking.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug5(db_state)
  INPUT: db_state of type DatabaseReadState
  OUTPUT: boolean
  
  RETURN db_state.read_attempted = TRUE
         AND db_state.error_occurred = TRUE
         AND db_state.error_type IN {corruption, locked, schema_mismatch}
         AND db_state.returned_default_zero = TRUE
         AND db_state.error_logged = FALSE
END FUNCTION
```

**Examples:**
- Database locked by backup → read fails → returns zeros → daily loss shows $0 when actually down $500 → loss limit never triggers
- Schema migration incomplete → read fails → returns zeros → trade count shows 0 when actually 45 → max_daily_trades ineffective

### Bug 6: No Database Backup

The bug manifests when system runs without creating automated backups. Database corruption or disk failure causes permanent data loss.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug6(system_state)
  INPUT: system_state of type TradingSystemState
  OUTPUT: boolean
  
  RETURN system_state.days_operational > 1
         AND system_state.backup_count = 0
         AND system_state.database_contains_trades = TRUE
END FUNCTION
```

**Examples:**
- 90 days operation → 450 trades → disk fails → all data lost → cannot recover performance analysis

### Bug 7: Missing Session Timeout

The bug manifests when dashboard session persists indefinitely without automatic logout. User walks away, session remains active, increasing unauthorized access risk.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug7(session)
  INPUT: session of type DashboardSession
  OUTPUT: boolean
  
  RETURN session.authenticated = TRUE
         AND (current_time - session.last_activity) > 30_minutes
         AND session.auto_logged_out = FALSE
END FUNCTION
```

**Examples:**
- User logs in 09:00, walks away → returns 15:00 → session still active → unauthorized access

### Bug 8: No Audit Logging

The bug manifests when security-relevant actions (engine control, settings changes, authentication) occur without audit trail. No record of who did what and when.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug8(action)
  INPUT: action of type DashboardAction
  OUTPUT: boolean
  
  RETURN action.type IN {auth_attempt, engine_control, settings_change, data_access}
         AND action.audit_logged = FALSE
END FUNCTION
```

**Examples:**
- User starts engine 08:00 → no audit entry → engine crashes 10:00 → cannot determine who started it
- Settings changed (risk_per_trade: 1% → 10%) → no record → cannot investigate excessive risk

### Bug 9: Spread Limit Not Strategy-Specific

The bug manifests when global `max_spread_points: 50.0` is applied to both strategies. Scalper (M1 timeframe) accepts spreads up to 50 points, which is suboptimal for scalping.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug9(entry_check)
  INPUT: entry_check of type EntryValidation
  OUTPUT: boolean
  
  RETURN entry_check.strategy = "scalper"
         AND entry_check.spread_points BETWEEN 20 AND 50
         AND entry_check.spread_check_passed = TRUE
         AND entry_check.using_global_limit = TRUE
END FUNCTION
```

**Examples:**
- Scalper signal with spread=35 points → global limit 50 → entry allowed → spread costs eat 70% of profit → trade loses money

### Bug 10: Duplicate .gitignore Entries

The bug manifests when `.gitignore` contains duplicate patterns, causing maintenance confusion.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug10(gitignore)
  INPUT: gitignore of type GitIgnoreFile
  OUTPUT: boolean
  
  RETURN has_duplicates(gitignore.entries)
END FUNCTION
```

**Examples:**
- `*.pyc` on line 15 and line 42 → developers unsure if both needed

### Bug 11: System Assumes Standard Account - Untradeable with Micro Capital

The bug manifests when system uses configuration defaults designed for $10,000 USD account, but actual account has R50-R100 ZAR (~$2.70-$5.40 USD). Position sizing, risk calculations, and margin requirements are fundamentally incorrect.

**Formal Specification:**
```
FUNCTION isBugCondition_Bug11(account_state)
  INPUT: account_state of type AccountState
  OUTPUT: boolean
  
  RETURN account_state.balance_zar BETWEEN 50 AND 100
         AND account_state.balance_usd < 6.0
         AND config.starting_balance = 10000.0
         AND minimum_viable_account_calculated = FALSE
END FUNCTION
```

**Examples:**
- R100 account (~$5.40 USD) → config assumes $10,000 → 1% risk = $100 calculated → exceeds entire balance → position sizing fails
- System tries 3 concurrent positions → each requires $50 margin → total $150 needed but only $5.40 available → all orders rejected
- XAUUSD volume_min 0.01 micro lots → system doesn't check if R100 can support even 1 trade with spread + commission
- Spread costs $0.50 per trade → that's 9.3% of R100 account → system doesn't report percentage → user unaware of cost impact

## Frequency & Profit-Capture Analysis

### 3.6 Micro-Account Cadence Feasibility

**Critical Reality Check:** The user requires scalping with 50-100 trades/day, but the actual account has R50-R100 ZAR (~$2.70-$5.40 USD). This subsection calculates whether this mandate is even possible.

#### Assumptions for Analysis
- **Account Balance**: R50-R100 ZAR
- **Exchange Rate**: 1 USD = R18.50 ZAR (approximate)
- **USD Equivalent**: $2.70-$5.40 USD
- **XAUUSD Price**: ~$2,300 USD/oz (current gold price)
- **Broker Type**: ZAR micro/cent account
- **volume_min**: 0.01 lots (1 oz gold, typical micro lot)
- **contract_size**: 100 oz per standard lot
- **Leverage**: 1:500 (typical for micro accounts)
- **Spread**: 30-50 points typical for XAUUSD on micro accounts
- **Commission**: R0.50-R2.00 per trade typical

#### Margin Calculation Per Trade

**Formula:**
```
margin_per_trade = (volume × contract_size × price) / leverage
margin_per_trade = (0.01 × 100 × 2300) / 500
margin_per_trade = 2300 / 500
margin_per_trade = $4.60 USD per 0.01 lot position
```

**In ZAR:**
```
margin_per_trade_zar = $4.60 × 18.50 = R85.10
```

**Critical Finding #1:** Opening even ONE 0.01 micro lot position on XAUUSD requires R85.10 margin, which EXCEEDS the entire R50-R100 account balance.

#### Spread Cost Per Trade

**Spread**: Assume 40 points average (0.40 USD price difference)
**Cost per 0.01 lot:**
```
spread_cost = spread_points × point_value × volume
spread_cost = 0.40 × 100 × 0.01
spread_cost = $0.40 USD per trade
```

**In ZAR:**
```
spread_cost_zar = $0.40 × 18.50 = R7.40 per trade
```

**As percentage of account:**
- R50 account: R7.40 / R50 = 14.8% per trade
- R100 account: R7.40 / R100 = 7.4% per trade

**Critical Finding #2:** Spread alone consumes 7.4-14.8% of the entire account per trade, making profitable trading nearly impossible.

#### Commission Cost Per Trade

Assume R1.00 commission per trade (conservative mid-range):

**As percentage of account:**
- R50 account: R1.00 / R50 = 2.0% per trade
- R100 account: R1.00 / R100 = 1.0% per trade

**Total transaction costs:**
- R50 account: 14.8% + 2.0% = 16.8% per trade
- R100 account: 7.4% + 1.0% = 8.4% per trade

**Critical Finding #3:** Transaction costs consume 8.4-16.8% of capital per trade BEFORE considering stop loss distance.

#### Concurrent Position Analysis

**Current config**: `max_open_positions = 3`

**Margin required for 3 concurrent 0.01 lot positions:**
```
total_margin = 3 × R85.10 = R255.30
```

**Available balance:** R50-R100

**Critical Finding #4:** The account cannot support even ONE position, let alone 3 concurrent positions. The margin requirement exceeds the total account balance by 2.5-5x.

#### Daily Trade Cadence Feasibility at 50-100 Trades/Day

**User Requirement**: Scalping with 50-100 trades/day

**Daily transaction costs at 50 trades/day:**
```
daily_spread_cost = 50 × R7.40 = R370 ZAR
daily_commission_cost = 50 × R1.00 = R50 ZAR
total_daily_costs = R420 ZAR
```

**Daily transaction costs at 100 trades/day:**
```
daily_spread_cost = 100 × R7.40 = R740 ZAR
daily_commission_cost = 100 × R1.00 = R100 ZAR
total_daily_costs = R840 ZAR
```

**Critical Finding #5:** 
- 50 trades/day would cost R420 in spreads + commission alone, which is 4.2-8.4x the entire account balance
- 100 trades/day would cost R840 in spreads + commission alone, which is 8.4-16.8x the entire account balance
- This is IMPOSSIBLE with R50-R100 capital

#### Recalculated Feasible Trade Capacity

**Working backward from R50-R100 balance:**

**Maximum supportable positions:**
```
max_positions = floor((balance_usd × leverage) / (volume_min × contract_size × price))
max_positions = floor(($5.40 × 500) / (0.01 × 100 × 2300))
max_positions = floor(2700 / 2300)
max_positions = floor(1.17)
max_positions = 1 position maximum (R100 account)
max_positions = 0 positions (R50 account)
```

**R50 account**: CANNOT place even 1 trade
**R100 account**: Can place 1 trade maximum, with no room for concurrent positions

**Daily trade cap considering transaction costs:**

Assuming 1 position at a time, and each trade costs 8.4% of R100 account:
```
sustainable_trades_per_day = account_balance / (avg_trade_cost × win_rate_requirement)
```

To not lose all capital to costs:
- Need 60%+ win rate just to cover 8.4% transaction costs
- Realistic sustainable trades: **3-5 trades/day maximum** on R100
- R50 account: **0-1 trades/day** (essentially untradeable)

#### Minimum Viable Account for Scalping (50-100 trades/day)

**Formula:**
```
min_viable_account = max(
  margin_for_max_positions,
  daily_transaction_costs / acceptable_cost_ratio,
  min_risk_budget
)
```

**Calculations for 50 trades/day:**

1. **Margin for 3 concurrent positions:** R255.30 ZAR
2. **Daily transaction costs:** R420 ZAR
3. **Acceptable cost ratio:** 2% of account max (industry standard)

```
min_account_for_costs = R420 / 0.02 = R21,000 ZAR (~$1,135 USD)
```

4. **Risk budget (1% per trade × 50 trades):**

```
min_account_for_risk = risk_per_trade_usd × trades × exchange_rate / risk_pct
// Assuming $5 USD minimum risk per trade for XAUUSD
min_account_for_risk = $5 × 50 × 18.50 / 0.01 = R46,250 ZAR (~$2,500 USD)
```

**Minimum Viable Account (50 trades/day):** R46,250 ZAR (~$2,500 USD)

**Calculations for 100 trades/day:**

1. **Daily transaction costs:** R840 ZAR
2. **Acceptable cost ratio:** 2% of account max

```
min_account_for_costs = R840 / 0.02 = R42,000 ZAR (~$2,270 USD)
```

3. **Risk budget (1% per trade × 100 trades):**

```
min_account_for_risk = $5 × 100 × 18.50 / 0.01 = R92,500 ZAR (~$5,000 USD)
```

**Minimum Viable Account (100 trades/day):** R92,500 ZAR (~$5,000 USD)

#### Verdict: Can R50-R100 Execute Scalping (50-100 Trades/Day)?

**NO. The mandate is IMPOSSIBLE at this account size.**

**What R50-R100 can actually do:**
- **R50 account**: UNTRADEABLE on XAUUSD micro lots (margin requirement R85.10 exceeds R50 balance)
- **R100 account**: Maximum 1 position, 3-5 trades/day sustainable, NOT 50-100 trades/day

**Required account size for user's scalping requirement:**
- **For 50 trades/day:** R46,250 ZAR minimum (~$2,500 USD)
- **For 100 trades/day:** R92,500 ZAR minimum (~$5,000 USD)

**VERDICT:**

The user's requirement for "many trades in short time" on R50-R100 capital is IMPOSSIBLE unless:
1. **Broker offers nano lots (0.001)** instead of micro lots (0.01) - reducing margin by 10x to R8.51 per position
2. **Account is increased** to minimum R46,250 ZAR for 50 trades/day or R92,500 ZAR for 100 trades/day
3. **Alternative instrument** with lower margin requirements is used (e.g., major currency pairs instead of XAUUSD)
4. **Lower leverage is accepted** with proportionally lower trade frequency (3-5 trades/day on R100 account)

**The system must:**
1. Calculate and DISPLAY this minimum viable account size for requested trade frequency
2. REFUSE to trade when balance < minimum for target frequency
3. REPORT maximum supportable trades/day given actual balance
4. LOG CRITICAL error: "Account balance R100 is below minimum R46,250 required for 50 trades/day. Maximum sustainable: 5 trades/day. Consider reducing target frequency or increasing capital."

This analysis must be integrated into Bug 11 fix as part of `account_validator.py`.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors (All Bugs):**
- Bug 1: When broker time is available, system continues using it for all trading decisions
- Bug 2: When user explicitly sets magic number, system continues using configured value
- Bug 3: Existing users with valid passwords continue authenticating successfully
- Bug 4: When valid model loads with correct checksum, system continues using it for filtering
- Bug 5: When database reads succeed, system continues using data for risk management
- Bug 6: Database operations during trading continue normally, backups don't block trading
- Bug 7: Active sessions within 30-minute window continue allowing dashboard access
- Bug 8: Audit logging doesn't block trading operations
- Bug 9: When spread within strategy-specific limits, entry continues being allowed
- Bug 10: All currently ignored files continue being ignored after cleanup
- Bug 11: When account has sufficient balance, system continues trading normally with existing position sizing

**Scope:**
Fixes affect only the specific bug conditions. Normal operation with valid inputs remains unchanged.

## Hypothesized Root Cause

**Bug 1:** Defensive programming taken too far - "keep trading at all costs" mentality created worse behavior than stopping when time source uncertain.

**Bug 2:** Copy-paste from MT5 tutorial code - 123456 is ubiquitous in examples, copied without considering collision risk.

**Bug 3:** MVP dashboard without security hardening - basic auth implemented without attack vector consideration.

**Bug 4:** ML pipeline focused on training, not deployment security - loading code assumes model always valid.

**Bug 5:** Try-except block too broad - catches all exceptions and returns zeros to "keep system running", conflating "no data" with "can't read data".

**Bug 6:** MVP focused on trading, not operations - database added for persistence without backup/recovery planning.

**Bug 7:** Flask session defaults used without customization - no inactivity timeout added.

**Bug 8:** Dashboard not considered security-sensitive - treated as local-only tool, not system controlling live money.

**Bug 9:** Configuration consolidated without strategy analysis - single max_spread_points created without recognizing scalper/breakout have different requirements.

**Bug 10:** Multiple contributors, merge conflicts, no linting - duplicates accumulated over time.

**Bug 11:** Configuration defaults from standard Forex assumptions - starting_balance: 10000.0 based on typical demo accounts, no consideration for micro/cent accounts with R50-R100.

## Correctness Properties

Property 1: Bug Condition - Broker Time Required for Trading

_For any_ trading engine state where broker time is unavailable (MT5 disconnected OR server timestamp is null), the fixed system SHALL immediately stop all trading operations and log a CRITICAL error, refusing to execute trades until broker time is restored.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Bug Condition - Unique Magic Number Generation

_For any_ first-time setup or configuration where no explicit magic number is set, the fixed system SHALL generate a unique 8-digit random magic number and SHALL reject common default values (123456, 111111, 999999) with a validation warning.

**Validates: Requirements 2.5, 2.6, 2.7**

Property 3: Bug Condition - Strong Password and Account Security

_For any_ password creation attempt, the fixed system SHALL enforce complexity requirements (12+ chars, uppercase, lowercase, digit, special char). _For any_ sequence of failed login attempts exceeding limits, the system SHALL enforce rate limiting and account lockout.

**Validates: Requirements 2.8, 2.9, 2.10, 2.11**

Property 4: Bug Condition - Model Integrity Verification

_For any_ ML model loading attempt when ml.enabled=True, the fixed system SHALL verify SHA-256 checksum. If verification fails, SHALL refuse to load model, log CRITICAL error, and block ML-gated trading.

**Validates: Requirements 2.12, 2.13, 2.14**

Property 5: Bug Condition - Explicit Database Error Handling

_For any_ database read operation in `_restore_day_state()` encountering an error, the fixed system SHALL log CRITICAL error and stop trading, distinguishing between "no records" (correct zeros) and "read error" (must stop).

**Validates: Requirements 2.15, 2.16, 2.17**

Property 6: Bug Condition - Automated Daily Backups

_For any_ trading day start, the fixed system SHALL create compressed backup before executing trades, rotate backups keeping most recent 30 days, and log WARNING (but continue trading) if backup fails.

**Validates: Requirements 2.18, 2.19, 2.20**

Property 7: Bug Condition - Session Timeout Implementation

_For any_ dashboard session with no activity for 30 minutes, the fixed system SHALL automatically log out user and redirect to login page. Any interaction after expiration SHALL require re-authentication.

**Validates: Requirements 2.21, 2.22, 2.23**

Property 8: Bug Condition - Comprehensive Audit Logging

_For any_ security-relevant action (authentication, engine control, settings change, data access), the fixed system SHALL create audit log entry with username, IP, timestamp, action type, and relevant details.

**Validates: Requirements 2.24, 2.25, 2.26, 2.27**

Property 9: Bug Condition - Strategy-Specific Spread Limits

_For any_ spread evaluation, the scalper strategy SHALL use 20-point limit, breakout strategy SHALL use 50-point limit. Each strategy SHALL have its own max_spread_points configuration.

**Validates: Requirements 2.28, 2.29, 2.30**

Property 10: Bug Condition - Deduplicated .gitignore

_For any_ maintenance of .gitignore, the fixed version SHALL contain no duplicate entries. Duplicates SHALL be consolidated.

**Validates: Requirements 2.31, 2.32**

Property 11: Bug Condition - Micro Capital Viability Validation

_For any_ system startup, the fixed system SHALL calculate minimum viable account size (ZAR and USD) considering margin, max_open_positions, risk_per_trade, leverage, volume_min, spread costs. If actual balance below minimum, SHALL refuse to trade and log CRITICAL error with required minimum. Position sizing and risk reporting SHALL work with ZAR denomination and dual-currency reporting.

**Validates: Requirements 2.33, 2.34, 2.35, 2.36, 2.37, 2.38, 2.39, 2.40**

Property 12: Preservation - Non-Buggy Input Behavior

_For any_ input where the bug condition does NOT hold for each bug, the fixed system SHALL produce the same result as the original system, preserving all existing functionality for normal operation.

**Validates: Requirements 3.1-3.33**

## Fix Implementation

### Changes Required

Assuming root cause analysis is correct, here are the specific implementation changes:

**Bug 1: Time Zone Fallback**
- **File**: `engine/trading_loop.py`
- **Function**: `get_broker_time()`
- **Changes**:
  1. Remove `datetime.now()` fallback when broker time unavailable
  2. Raise `BrokerTimeUnavailableError` exception instead
  3. Add try-except in trading loop to catch exception and stop trading
  4. Log CRITICAL error indicating trading cannot continue
  5. Add recovery logic to resume when broker time restored

**Bug 2: Magic Number Collision**
- **File**: `config/settings.py`
- **Class**: `MT5Config`
- **Changes**:
  1. Change `magic: int = 123456` to `magic: int = field(default_factory=_generate_unique_magic)`
  2. Create `_generate_unique_magic()` function returning random 8-digit number avoiding common defaults
  3. Add validation warning if user sets collision-prone magic
  4. Persist generated magic to file for consistency across restarts

**Bug 3: Weak Password Validation**
- **Files**: `dashboard/auth.py`, `dashboard/models.py`
- **Functions**: `create_user()`, `attempt_login()`
- **Changes**:
  1. Add `validate_password_strength()` function checking 12+ chars, uppercase, lowercase, digit, special
  2. Add IP-based rate limiting tracking attempts per 15-minute window
  3. Add account lockout tracking consecutive failures, lock after 5 for 15 minutes
  4. Integrate checks into login flow with appropriate error messages

**Bug 4: Model Integrity Verification**
- **Files**: `ml/model.py`, `ml/train.py`
- **Functions**: `load_model()`, `save_model()`
- **Changes**:
  1. On save: calculate SHA-256 checksum, store in `models/checksums.json` registry
  2. On load: verify file checksum against registry, raise `ModelVerificationError` if mismatch
  3. In trading engine: catch verification error, log CRITICAL, prevent startup when ml.enabled=True

**Bug 5: Silent Error Recovery**
- **File**: `engine/risk_manager.py`
- **Function**: `_restore_day_state()`
- **Changes**:
  1. Separate try-except for query execution vs. empty result
  2. On empty result: return zeros (correct - fresh day)
  3. On exception: log CRITICAL error, raise `DatabaseReadError`, stop trading
  4. Create specific exception types for different error categories

**Bug 6: Database Backup**
- **Files**: `database/backup.py` (new), `engine/trading_loop.py`
- **Functions**: `create_backup()`, `rotate_backups()`
- **Changes**:
  1. Create `create_backup()` using `shutil.make_archive()` with gzip compression
  2. Create `rotate_backups()` deleting files older than 30 days
  3. Call at day boundary before first trade
  4. Log WARNING on failure but continue trading

**Bug 7: Session Timeout**
- **File**: `dashboard/app.py`
- **Configuration**: Flask session config and before_request handler
- **Changes**:
  1. Set `app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)`
  2. Add `@app.before_request` checking `last_activity` timestamp
  3. Clear session and redirect to login if >30 minutes inactive
  4. Update `last_activity` on each request

**Bug 8: Audit Logging**
- **Files**: `dashboard/audit.py` (new), integrate into routes
- **Function**: `log_audit_event()`
- **Changes**:
  1. Create audit logger writing to `logs/audit.log`
  2. Add `log_audit_event(event_type, username, details)` function
  3. Integrate into authentication (success/failure), engine control, settings changes
  4. Format: timestamp | event_type | user | ip | details

**Bug 9: Strategy-Specific Spread Limits**
- **File**: `config/settings.py`
- **Classes**: `BreakoutConfig`, `ScalperConfig`, `ExecutionConfig`
- **Changes**:
  1. Remove `max_spread_points` from `ExecutionConfig`
  2. Add `max_spread_points: float = 50.0` to `BreakoutConfig`
  3. Add `max_spread_points: float = 20.0` to `ScalperConfig`
  4. Update strategy entry checks to use `CONFIG.{strategy}.max_spread_points`

**Bug 10: .gitignore Duplicates**
- **File**: `.gitignore`
- **Changes**:
  1. Read file, identify duplicates
  2. Deduplicate keeping first occurrence with comments
  3. Organize by category (Python, IDE, Logs, Database, etc.)
  4. Verify all patterns still work after cleanup

**Bug 11: Micro Capital Viability**
- **Files**: `config/settings.py`, `engine/account_validator.py` (new), `engine/trading_loop.py`
- **Functions**: `calculate_minimum_viable_account()`, `validate_account_capital()`
- **Changes**:
  1. Create `calculate_minimum_viable_account()`: query MT5 for symbol volume_min, margin_rate, leverage; calculate margin_per_position = (volume_min × contract_size × price) / leverage; total_margin = max_open_positions × margin_per_position × 1.2 (safety buffer); min_balance_for_risk = min_risk_usd / risk_per_trade; return max(total_margin, min_balance_for_risk)
  2. Create `validate_account_capital()`: get account balance from MT5 in native currency; if ZAR, convert to USD using USDZAR rate; call calculate_minimum_viable_account(); compare actual vs. minimum; calculate max_supportable_positions; return AccountViability object with can_trade flag
  3. Integrate into trading engine startup: call validate_account_capital(); if !can_trade, log CRITICAL with required minimum in ZAR and USD, raise InsufficientCapitalError; log viability details (balance, minimum, risk per trade, spread cost, max positions)
  4. Add dual-currency logging throughout position sizing and risk reporting
  5. Add spread cost percentage calculation: (spread_cost_usd / account_balance_usd) × 100

## Testing Strategy

### Validation Approach

Three-phase approach:
1. **Exploratory**: Reproduce each bug on unfixed code to confirm root cause
2. **Fix Verification**: Verify fixed code corrects the bug for all buggy inputs
3. **Preservation**: Verify normal operation unchanged for non-buggy inputs

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples demonstrating each bug BEFORE implementing fixes.

**Bug 1**: Disconnect MT5 mid-session → system uses PC time → day boundary incorrect (WILL FAIL)
**Bug 2**: Two strategies both magic=123456 → cannot distinguish positions (WILL FAIL)
**Bug 3**: Create password "password123" → accepted (WILL FAIL); 100 login attempts → all allowed (WILL FAIL)
**Bug 4**: Corrupt model file → loads without error (WILL FAIL)
**Bug 5**: Lock database → `_restore_day_state()` returns zeros, no error (WILL FAIL)
**Bug 6**: Run 7 days → no backups exist (WILL FAIL)
**Bug 7**: Log in, wait 60 minutes → session still active (WILL FAIL)
**Bug 8**: Start engine → no audit entry (WILL FAIL)
**Bug 9**: Scalper signal spread=35 → allowed by global 50 limit (WILL FAIL)
**Bug 10**: Search .gitignore → find duplicates (WILL FAIL)
**Bug 11**: Initialize with R100 account → calculates $100 risk exceeding balance (WILL FAIL); attempts 3 positions → margin requirement $150 > $5.40 (WILL FAIL)

### Fix Checking

**Goal**: Verify for all inputs where bug condition holds, fixed function produces expected correct behavior.

**Pseudocode (example for Bug 1):**
```
FOR ALL trading_state WHERE broker_time_unavailable(trading_state) DO
  ASSERT fixed_system.trading_active = FALSE
  ASSERT "TRADING STOPPED" IN critical_logs
  ASSERT NOT using_fallback_time(trading_state)
END FOR
```

Similar pseudocode for all 11 bugs verifying Property 1-11.

### Preservation Checking

**Goal**: Verify for all inputs where bug condition does NOT hold, fixed function produces same result as original.

**Pseudocode (example for Bug 1):**
```
FOR ALL trading_states WHERE broker_time_available DO
  ASSERT fixed_system.uses_broker_time = original_system.uses_broker_time
  ASSERT fixed_system.day_boundaries = original_system.day_boundaries
END FOR
```

Similar pseudocode for all 11 bugs verifying Property 12.

### Unit Tests

Each bug SHALL have dedicated unit tests covering:
- Bug condition recognition (isBugCondition returns true)
- Expected behavior enforcement (fix produces correct output)
- Preservation of normal operation (non-buggy inputs unchanged)
- Edge cases specific to each bug

### Property-Based Tests

Property-based testing STRONGLY RECOMMENDED for:
- **Bug 3**: Generate random password strings, verify complexity enforced
- **Bug 5**: Generate random database states (empty, valid, corrupted), verify correct handling
- **Bug 11**: Generate random account balances and configurations, verify viability calculations

### Integration Tests

Integration tests SHALL verify:
- **Bug 1**: Full trading loop with MT5 connection failures
- **Bug 2**: Multiple strategies with unique magic numbers
- **Bug 3**: Complete authentication flow with rate limiting
- **Bug 4**: ML model loading in startup sequence
- **Bug 5**: Day state restoration during initialization
- **Bug 6**: Backup creation across multiple trading days
- **Bug 7**: Session lifecycle from login to auto-logout
- **Bug 8**: Audit log entries for complete session
- **Bug 9**: Strategy execution with spread filtering
- **Bug 11**: Complete engine initialization with capital validation
