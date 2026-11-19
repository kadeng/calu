# The SSH box: a headless server for the PyCharm plugin's SSH mode (requirement 35). Linux with
# `sshd` (public-key login only), calu, PostgreSQL and a scripted `calu serve`; no desktop.
# See ../README.md, "The SSH box".
#
#   dev   uid 1000, the only login; sudo without a password (inside this container only)
#
# The image holds no key. `run.sh` installs the public key of a pair made for the tests into
# /home/dev/.ssh/authorized_keys when it creates the container.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server sudo git tmux curl ca-certificates procps less iproute2 \
        postgresql postgresql-client locales \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8 \
    && mkdir -p /run/sshd
ENV LANG=en_US.UTF-8

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /uvx /usr/local/bin/

# Ubuntu's image has a user `ubuntu` with uid 1000; the box wants that uid for dev. The
# password field '*' (no password matches) instead of useradd's '!' (locked): sshd without
# PAM refuses every login of a locked account, also one with a key.
RUN userdel -r ubuntu \
    && useradd -m -u 1000 -s /bin/bash -G sudo dev \
    && usermod -p '*' dev \
    && echo 'dev ALL=(ALL:ALL) NOPASSWD: ALL' >/etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev \
    && install -d -m 0700 -o dev -g dev /home/dev/.ssh

# Public keys only: no password, no keyboard-interactive, no root login.
RUN printf '%s\n' \
        'PasswordAuthentication no' \
        'KbdInteractiveAuthentication no' \
        'PubkeyAuthentication yes' \
        'PermitRootLogin no' \
        'AllowUsers dev' \
        'UsePAM no' \
        'PrintMotd no' \
        >/etc/ssh/sshd_config.d/10-sshbox.conf

# A second cluster on port 5433 for the store with passwords (calu's roles are server-wide).
RUN pg_createcluster 16 pw -p 5433

COPY sshbox-init sshbox-install sshbox-server /usr/local/sbin/
RUN chmod 0755 /usr/local/sbin/sshbox-init /usr/local/sbin/sshbox-install /usr/local/sbin/sshbox-server

EXPOSE 22
CMD ["/usr/local/sbin/sshbox-init"]
