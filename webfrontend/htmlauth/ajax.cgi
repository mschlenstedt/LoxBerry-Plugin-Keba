#!/usr/bin/perl
# KEBA-Plugin: JSON-Schnittstelle für die WebUI (action=…).
# Passwörter werden nie an den Browser zurückgegeben und nur über stdin an keba_cli.py gereicht.

use strict;
use warnings;
use utf8;
use CGI;
use JSON::PP;
use IPC::Open3;
use Symbol qw(gensym);
use Encode qw(decode);
use File::Temp qw(tempfile);
use File::Basename qw(dirname);
use LoxBerry::System;

my $cgi    = CGI->new;
my $action = $cgi->param('action') // '';

my $SHMDIR       = '/dev/shm/keba';
my $PID_FILE     = '/dev/shm/keba_gateway.pid';
my $CFG_FILE     = "$lbpconfigdir/pluginconfig.json";
my $STOPPED_FLAG = "$lbpconfigdir/gateway_stopped";
my $CLI          = "$lbpbindir/keba_cli.py";
my $FOLDER       = $lbpplugindir;
$FOLDER =~ s{.*/}{};
my $DAEMON       = "$lbhomedir/system/daemons/plugins/$FOLDER";
my $MIN_INTERVAL = 5;

my $JSON = JSON::PP->new->utf8->canonical;

my %actions = (
    getstatus      => \&action_getstatus,
    start          => \&action_start,
    stop           => \&action_stop,
    restart        => \&action_restart,
    getconfig      => \&action_getconfig,
    savebox        => \&action_savebox,
    deletebox      => \&action_deletebox,
    checkbox       => \&action_checkbox,
    savemqtt       => \&action_savemqtt,
    getoperations  => \&action_getoperations,
    setop          => \&action_setop,
    refreshopenapi => \&action_refreshopenapi,
    diag           => \&action_diag,
);

print $cgi->header(-type => 'application/json', -charset => 'UTF-8');
if (my $handler = $actions{$action}) {
    my $result = eval { $handler->() };
    $result = { ok => JSON::PP::false, error => "Interner Fehler: $@" } if $@;
    print $JSON->encode($result);
} else {
    print $JSON->encode({ ok => JSON::PP::false, error => "Unbekannte Aktion: $action" });
}
exit;

# ---- Hilfen ------------------------------------------------------------------------------------
sub param {
    my ($name, $default) = @_;
    my $v = $cgi->param($name);
    return $default unless defined $v;
    $v = decode('UTF-8', $v);
    $v =~ s/^\s+|\s+$//g;
    return $v;
}

sub fail { return { ok => JSON::PP::false, error => $_[0] }; }

sub read_pid {
    return undef unless -f $PID_FILE;
    open(my $fh, '<', $PID_FILE) or return undef;
    my $pid = <$fh> // '';
    close $fh;
    $pid =~ s/\D//g;
    return $pid ne '' ? $pid : undef;
}

sub gateway_running {
    my $pid = read_pid();
    return (defined $pid && kill(0, $pid)) ? $pid : undef;
}

sub reload_gateway {
    my $pid = gateway_running();
    kill('HUP', $pid) if $pid;
    return $pid ? 1 : 0;
}

sub read_json_file {
    my ($file) = @_;
    return undef unless -f $file;
    open(my $fh, '<:raw', $file) or return undef;
    local $/;
    my $raw = <$fh>;
    close $fh;
    my $data = eval { JSON::PP->new->utf8->decode($raw) };
    return $data;
}

sub read_cfg {
    my $cfg = read_json_file($CFG_FILE) // {};
    $cfg = {} unless ref $cfg eq 'HASH';
    my $m = ref $cfg->{mqtt} eq 'HASH' ? $cfg->{mqtt} : {};
    $cfg->{mqtt} = {
        base_topic       => (defined $m->{base_topic} && $m->{base_topic} ne '') ? $m->{base_topic} : 'keba',
        core_interval    => int($m->{core_interval} // 10),
        default_interval => int($m->{default_interval} // 60),
        retain           => (!defined $m->{retain} || $m->{retain}) ? JSON::PP::true : JSON::PP::false,
    };
    my @boxes;
    for my $b (@{ ref $cfg->{boxes} eq 'ARRAY' ? $cfg->{boxes} : [] }) {
        next unless ref $b eq 'HASH' && defined $b->{id} && $b->{id} ne '';
        my @enabled;
        for my $e (@{ ref $b->{enabled} eq 'ARRAY' ? $b->{enabled} : [] }) {
            next unless ref $e eq 'HASH' && defined $e->{op} && $e->{op} ne '';
            push @enabled, { op => "$e->{op}",
                             interval => defined $e->{interval} ? int($e->{interval}) : undef };
        }
        push @boxes, {
            id       => "$b->{id}",
            name     => $b->{name} // '',
            host     => $b->{host} // '',
            username => $b->{username} // '',
            password => $b->{password} // '',
            active   => (!defined $b->{active} || $b->{active}) ? JSON::PP::true : JSON::PP::false,
            serial   => $b->{serial} // '',
            enabled  => \@enabled,
        };
    }
    $cfg->{boxes} = \@boxes;
    return $cfg;
}

# Atomar schreiben, Modus 600 (die Datei enthält Passwörter).
sub write_cfg {
    my ($cfg) = @_;
    my $data = JSON::PP->new->utf8->canonical->pretty->encode({ mqtt => $cfg->{mqtt}, boxes => $cfg->{boxes} });
    my ($fh, $tmp) = tempfile('.pluginconfig.XXXXXX', DIR => dirname($CFG_FILE));
    binmode $fh;
    print $fh $data;
    close $fh or do { unlink $tmp; return 0 };
    chmod 0600, $tmp;
    rename($tmp, $CFG_FILE) or do { unlink $tmp; return 0 };
    return 1;
}

sub find_box {
    my ($cfg, $id) = @_;
    for my $b (@{ $cfg->{boxes} }) {
        return $b if $b->{id} eq $id;
    }
    return undef;
}

# keba_cli.py aufrufen; $stdin geht über eine Pipe, nie über die Kommandozeile.
sub run_cli {
    my ($stdin, @args) = @_;
    return fail("keba_cli.py fehlt") unless -f $CLI;
    local $SIG{PIPE} = 'IGNORE';
    my ($in, $out);
    my $err = gensym;
    my $pid = eval {
        open3($in, $out, $err, 'python3', $CLI, '--configdir', $lbpconfigdir,
              '--shmdir', $SHMDIR, '--logdir', $lbplogdir, @args);
    };
    return fail("keba_cli.py nicht startbar: $@") unless $pid;
    print $in ($stdin // '');
    close $in;
    my $text = do { local $/; <$out> } // '';
    close $out;
    waitpid($pid, 0);
    my $result = eval { JSON::PP->new->utf8->decode($text) };
    return ref $result eq 'HASH' ? $result : fail('keba_cli.py lieferte kein JSON');
}

sub valid_host { return $_[0] =~ /^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$/; }

sub wait_for_pid {
    for (1 .. 12) {
        select(undef, undef, undef, 0.5);
        my $pid = gateway_running();
        return $pid if $pid;
    }
    return undef;
}

sub kill_gateway {
    my $pid = gateway_running();
    if ($pid) {
        kill('TERM', $pid);
        for (1 .. 10) {
            sleep 1;
            last unless kill(0, $pid);
        }
        kill('KILL', $pid) if kill(0, $pid);
    }
    unlink $PID_FILE if -f $PID_FILE;
}

sub start_gateway {
    unlink $STOPPED_FLAG if -f $STOPPED_FLAG;
    return fail("Daemon-Skript fehlt: $DAEMON") unless -x $DAEMON;
    system($DAEMON, 'start');
    my $pid = wait_for_pid();
    return $pid ? { ok => JSON::PP::true, pid => $pid + 0 } : fail('Gateway ist nicht gestartet');
}

# ---- Aktionen ----------------------------------------------------------------------------------
sub action_getstatus {
    my $status = read_json_file("$SHMDIR/status.json");
    $status = {} unless ref $status eq 'HASH';
    my $pid = gateway_running();
    $status->{running}      = $pid ? JSON::PP::true : JSON::PP::false;
    $status->{pid}          = $pid ? $pid + 0 : undef;
    $status->{stopped_flag} = -f $STOPPED_FLAG ? JSON::PP::true : JSON::PP::false;
    $status->{watchdog_gaveup} = -f "$SHMDIR/watchdog.gaveup" ? JSON::PP::true : JSON::PP::false;
    $status->{ok} = JSON::PP::true;
    return $status;
}

sub action_start {
    return { ok => JSON::PP::true, pid => gateway_running() + 0, msg => 'läuft bereits' }
        if gateway_running();
    return start_gateway();
}

sub action_restart {
    kill_gateway();
    return start_gateway();
}

sub action_stop {
    if (open(my $fh, '>', $STOPPED_FLAG)) { close $fh; }
    kill_gateway();
    return { ok => JSON::PP::true };
}

sub action_getconfig {
    my $cfg = read_cfg();
    for my $b (@{ $cfg->{boxes} }) {
        $b->{password_set} = $b->{password} ne '' ? JSON::PP::true : JSON::PP::false;
        delete $b->{password};
    }
    return { ok => JSON::PP::true, mqtt => $cfg->{mqtt}, boxes => $cfg->{boxes} };
}

sub action_checkbox {
    my $id       = param('id', '');
    my $host     = param('host', '');
    my $username = param('username', '');
    my $password = $cgi->param('password') // '';
    $password = decode('UTF-8', $password);
    if ($id ne '') {
        my $box = find_box(read_cfg(), $id) or return fail('Box nicht gefunden');
        $host     = $box->{host}     if $host eq '';
        $username = $box->{username} if $username eq '';
        $password = $box->{password} if $password eq '';
    }
    return fail('Ungültige Adresse') unless valid_host($host);
    my $res = run_cli(Encode::encode('UTF-8', $password), 'check', '--host', $host,
                      '--username', ($username ne '' ? $username : 'admin'), '--password-stdin');
    $res->{ok} = JSON::PP::true unless exists $res->{ok};
    return $res;
}

sub action_savebox {
    my $cfg      = read_cfg();
    my $id       = param('id', '');
    my $name     = param('name', '');
    my $host     = param('host', '');
    my $username = param('username', '');
    my $password = decode('UTF-8', $cgi->param('password') // '');
    my $active   = param('active', '1');

    return fail('Ungültige Adresse') unless valid_host($host);
    return fail('Name zu lang') if length($name) > 64;
    $username = 'admin' if $username eq '';

    my $box;
    if ($id ne '') {
        $box = find_box($cfg, $id) or return fail('Box nicht gefunden');
    } else {
        my %used = map { $_->{id} => 1 } @{ $cfg->{boxes} };
        my $n = 1;
        $n++ while $used{"b$n"};
        $box = { id => "b$n", serial => '', enabled => [], password => '' };
        push @{ $cfg->{boxes} }, $box;
    }
    $password = $box->{password} if $password eq '';
    return fail('Passwort fehlt') if $password eq '';

    my $check = run_cli(Encode::encode('UTF-8', $password), 'check', '--host', $host,
                        '--username', $username, '--password-stdin');
    my $serial = $check->{serial} // '';
    if ($serial ne '') {
        for my $other (@{ $cfg->{boxes} }) {
            next if $other == $box;
            return fail("Diese Wallbox ($other->{name}) ist bereits eingerichtet")
                if ($other->{serial} // '') eq $serial;
        }
        $box->{serial} = $serial;
    }
    $box->{name}     = $name ne '' ? $name : ($box->{name} || "Wallbox $box->{id}");
    $box->{host}     = $host;
    $box->{username} = $username;
    $box->{password} = $password;
    $box->{active}   = ($active eq '0' || $active eq 'false') ? JSON::PP::false : JSON::PP::true;

    write_cfg($cfg) or return fail('Konfiguration konnte nicht gespeichert werden');
    my $reloaded = reload_gateway();
    return { ok => JSON::PP::true, id => $box->{id}, serial => $box->{serial},
             reloaded => $reloaded ? JSON::PP::true : JSON::PP::false, check => $check };
}

sub action_deletebox {
    my $id  = param('id', '');
    my $cfg = read_cfg();
    my @keep = grep { $_->{id} ne $id } @{ $cfg->{boxes} };
    return fail('Box nicht gefunden') if @keep == @{ $cfg->{boxes} };
    $cfg->{boxes} = \@keep;
    write_cfg($cfg) or return fail('Konfiguration konnte nicht gespeichert werden');
    reload_gateway();
    return { ok => JSON::PP::true };
}

sub action_savemqtt {
    my $cfg  = read_cfg();
    my $base = param('base_topic', $cfg->{mqtt}{base_topic});
    $base =~ s{^/+|/+$}{}g;
    return fail('Basis-Topic ungültig (keine Leerzeichen, # oder +)')
        if $base eq '' || $base =~ m{[\s#+]} || length($base) > 100;
    my %iv;
    for my $k (qw(core_interval default_interval)) {
        my $v = param($k, $cfg->{mqtt}{$k});
        return fail("$k muss eine ganze Zahl sein") unless "$v" =~ /^\d+$/;
        return fail("$k mindestens $MIN_INTERVAL s") if $v < $MIN_INTERVAL;
        $iv{$k} = int($v);
    }
    my $retain = param('retain', $cfg->{mqtt}{retain} ? '1' : '0');
    $cfg->{mqtt} = {
        base_topic => $base, %iv,
        retain => ($retain eq '0' || $retain eq 'false') ? JSON::PP::false : JSON::PP::true,
    };
    write_cfg($cfg) or return fail('Konfiguration konnte nicht gespeichert werden');
    reload_gateway();
    return { ok => JSON::PP::true, mqtt => $cfg->{mqtt} };
}

sub action_getoperations {
    my $id = param('id', '');
    return fail('Box fehlt') if $id eq '';
    my $res = run_cli('', 'operations', '--box-id', $id);
    $res->{ok} = $res->{error} ? JSON::PP::false : JSON::PP::true;
    return $res;
}

sub action_setop {
    my $id       = param('id', '');
    my $op       = param('op', '');
    my $enabled  = param('enabled', '1');
    my $interval = param('interval', '');
    my $cfg = read_cfg();
    my $box = find_box($cfg, $id) or return fail('Box nicht gefunden');
    return fail('Operation fehlt') if $op eq '';
    my $on = !($enabled eq '0' || $enabled eq 'false');
    if ($interval ne '') {
        return fail('Zyklus muss eine ganze Zahl sein') unless $interval =~ /^\d+$/;
        return fail("Zyklus mindestens $MIN_INTERVAL s") if $interval < $MIN_INTERVAL;
    }
    if ($on) {
        my $ops = run_cli('', 'operations', '--box-id', $id);
        return fail($ops->{error}) if $ops->{error};
        my ($found) = grep { $_->{op} eq $op } @{ $ops->{ops} || [] };
        return fail('Operation in dieser Firmware unbekannt') unless $found;
        return fail("Gesperrt: $found->{block_reason}") if $found->{status} eq 'blocked';
        return fail('Kern-Operation ist immer aktiv') if $found->{status} eq 'core';
    }
    my @enabled = grep { $_->{op} ne $op } @{ $box->{enabled} };
    push @enabled, { op => $op, interval => ($interval ne '' ? int($interval) : undef) } if $on;
    $box->{enabled} = \@enabled;
    write_cfg($cfg) or return fail('Konfiguration konnte nicht gespeichert werden');
    reload_gateway();
    return { ok => JSON::PP::true, enabled => $box->{enabled} };
}

sub action_refreshopenapi {
    my $id  = param('id', '');
    my $box = find_box(read_cfg(), $id) or return fail('Box nicht gefunden');
    if (($box->{serial} // '') =~ /^[A-Za-z0-9_-]+$/) {
        unlink "$SHMDIR/$box->{serial}/openapi.json", "$SHMDIR/$box->{serial}/version";
    }
    # Der Worker liest die openapi.json nur beim Einrichten oder Versionswechsel neu ein.
    if (gateway_running()) {
        kill_gateway();
        return start_gateway();
    }
    return { ok => JSON::PP::true };
}

sub action_diag {
    my $res = run_cli('', 'diag');
    $res->{ok} = $res->{error} ? JSON::PP::false : JSON::PP::true;
    return $res;
}
