-- 06-5 (1/2): 정형 업무 데이터 — 노트클라우드의 기존 운영 DB에 해당하는 부분
-- 이 테이블들은 "온톨로지 이전"에도 회사에 이미 있었을 데이터입니다.

DROP TABLE IF EXISTS team_members, usage_stats, payments, subscriptions, customers, incidents CASCADE;

CREATE TABLE customers (
    customer_id        text PRIMARY KEY,
    display_name       text NOT NULL,
    customer_type      text NOT NULL CHECK (customer_type IN ('individual', 'team')),
    is_student         boolean NOT NULL DEFAULT false,
    account_status     text NOT NULL CHECK (account_status IN ('active', 'suspended')),
    suspended_at       date,
    suspension_reason  text
);

CREATE TABLE subscriptions (
    subscription_id        text PRIMARY KEY,
    customer_id            text NOT NULL REFERENCES customers,
    plan                   text NOT NULL CHECK (plan IN ('basic', 'pro', 'team', 'student')),
    billing_cycle          text NOT NULL CHECK (billing_cycle IN ('monthly', 'yearly')),
    seats                  int  NOT NULL DEFAULT 1,
    first_paid_at          date NOT NULL,
    current_period_start   date NOT NULL,
    current_period_end     date NOT NULL,
    promo_code             text,
    status                 text NOT NULL CHECK (status IN ('active', 'cancel_scheduled', 'ended')),
    student_verified_until date
);

CREATE TABLE payments (
    payment_id      text PRIMARY KEY,
    subscription_id text NOT NULL REFERENCES subscriptions,
    paid_at         date NOT NULL,
    amount          int  NOT NULL,
    list_price      int  NOT NULL,
    promo_code      text,
    is_renewal      boolean NOT NULL,
    payment_method  text NOT NULL
);

CREATE TABLE usage_stats (
    subscription_id text NOT NULL REFERENCES subscriptions,
    period_start    date NOT NULL,
    docs_created    int  NOT NULL,
    logins          int  NOT NULL,
    PRIMARY KEY (subscription_id, period_start)
);

CREATE TABLE incidents (
    incident_id            text PRIMARY KEY,
    started_at             timestamp NOT NULL,
    ended_at               timestamp NOT NULL,
    duration_hours         int NOT NULL,
    responsibility         text NOT NULL,
    is_planned_maintenance boolean NOT NULL,
    announced_at           timestamp
);

CREATE TABLE team_members (
    customer_id text NOT NULL REFERENCES customers,
    member_name text NOT NULL,
    role        text NOT NULL CHECK (role IN ('admin', 'member')),
    PRIMARY KEY (customer_id, member_name)
);
