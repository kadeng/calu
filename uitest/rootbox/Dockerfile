# The root box: a throwaway machine with root, sudo and three users, for running calu's
# privileged paths for real (calu as root with --user, through sudo, with and without
# passwords). See ../README.md, "The root box".
#
#   root   the administrator; calu installed for root in /usr/local/lib/calu
#   ada    uid 1000, sudo without a password; a second calu installed in her home
#   bob    uid 1001, sudo asks his password ("bob-sudo")
#   carol  uid 1002, no sudo: another user of the machine
#
# No Docker inside (calu's `docker` database variant cannot run here); PostgreSQL 16 from
# Ubuntu is the "existing server" variant, started without systemd by rootbox-init: one
# cluster on port 5432 for root's calu, one on 5433 for a user's own.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        sudo git tmux bubblewrap socat curl ca-certificates procps less acl \
        postgresql postgresql-client locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8
ENV LANG=en_US.UTF-8

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /usr/local/bin/

# Ubuntu's image has a user `ubuntu` with uid 1000; the box wants that uid for ada.
RUN userdel -r ubuntu \
    && useradd -m -u 1000 -s /bin/bash -G sudo ada \
    && useradd -m -u 1001 -s /bin/bash -G sudo bob \
    && useradd -m -u 1002 -s /bin/bash carol \
    && echo 'bob:bob-sudo' | chpasswd \
    && echo 'ada ALL=(ALL:ALL) NOPASSWD: ALL' >/etc/sudoers.d/ada \
    && chmod 0440 /etc/sudoers.d/ada

# A second cluster on port 5433 for a user's own setup: calu's roles belong to a whole
# server, so root's setup and ada's cannot share one.
RUN pg_createcluster 16 user -p 5433

COPY rootbox-init rootbox-install rootbox-reset rootbox-tests claude-standin /usr/local/sbin/
COPY ptyrun webcheck idedrive /usr/local/bin/
RUN chmod 0755 /usr/local/sbin/rootbox-init /usr/local/sbin/rootbox-install \
        /usr/local/sbin/rootbox-reset /usr/local/sbin/rootbox-tests \
        /usr/local/sbin/claude-standin \
        /usr/local/bin/ptyrun /usr/local/bin/webcheck /usr/local/bin/idedrive \
    && ln -s /usr/local/sbin/claude-standin /usr/local/bin/claude \
    && install -d -m 1777 /var/tmp/standin \
    && echo 'export PATH="$HOME/.local/bin:$PATH"' >>/home/ada/.profile

CMD ["/usr/local/sbin/rootbox-init"]
