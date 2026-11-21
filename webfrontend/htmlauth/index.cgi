#!/usr/bin/perl
# KEBA-Plugin: WebUI im LoxBerry Design System ("nojqm"), fünf Tabs über ?form=.
# Alle Daten holt das gemeinsame JavaScript über ajax.cgi.

use strict;
use warnings;
use CGI;
use HTML::Template;
use JSON::PP;
use LoxBerry::System;
use LoxBerry::Web;
use LoxBerry::Log;

my $cgi = CGI->new;
my $q   = $cgi->Vars;

my $version = LoxBerry::System::pluginversion() // '';
my $WIKI    = 'https://wiki.loxberry.de/plugins/keba/start';

my %forms = (
    overview => 'tab_overview.html',
    settings => 'tab_settings.html',
    mqtt     => 'tab_mqtt.html',
    docs     => 'tab_docs.html',
    logfiles => 'tab_logfiles.html',
);
my $form = (defined $q->{form} && $forms{ $q->{form} }) ? $q->{form} : 'overview';

my $template = LoxBerry::System::read_file("$lbptemplatedir/$forms{$form}");
# Das gemeinsame JavaScript läuft mit durch HTML::Template (Sprachtexte).
$template .= LoxBerry::System::read_file("$lbptemplatedir/javascript.js");

my $templateout = HTML::Template->new_scalar_ref(
    \$template,
    global_vars       => 1,
    loop_context_vars => 1,
    die_on_bad_params => 0,
);
my %L = LoxBerry::System::readlanguage($templateout, 'language.ini');

# Alle Texte als ein JSON-Objekt für das JavaScript; "</" entschärfen.
my $lang_json = JSON::PP->new->canonical->encode(\%L);
$lang_json =~ s{</}{<\\/}g;
$templateout->param(
    LANG_JSON => $lang_json,
    WIKI_URL  => $WIKI,
    FORM      => $form,
    VERSION   => $version,
);

our %navbar;
my @tabs = ([10, 'overview', 'COMMON.TAB_OVERVIEW'], [20, 'settings', 'COMMON.TAB_SETTINGS'],
            [30, 'mqtt', 'COMMON.TAB_MQTT'], [40, 'docs', 'COMMON.TAB_DOCS'],
            [50, 'logfiles', 'COMMON.TAB_LOGFILES']);
for my $t (@tabs) {
    my ($nr, $name, $key) = @$t;
    $navbar{$nr}{Name}   = $L{$key};
    $navbar{$nr}{URL}    = "index.cgi?form=$name";
    $navbar{$nr}{active} = 1 if $form eq $name;
}

if ($form eq 'logfiles') {
    $templateout->param(LOGLIST => LoxBerry::Web::loglist_html());
}

# SecurePIN-Abfrage mit den Texten des Cores (eigene Vorlage, damit sich die COMMON-Abschnitte
# von Core und Plugin nicht überschreiben). Der Seiteninhalt bleibt unsichtbar, bis das
# JavaScript die PIN geprüft hat; ajax.cgi liefert ohne gültige Sitzung ohnehin nichts.
my $pintemplate = HTML::Template->new(
    filename          => "$lbptemplatedir/securepin.html",
    die_on_bad_params => 0,
);
LoxBerry::System::readlanguage($pintemplate, undef, 1);

LoxBerry::Web::lbheader("$L{'COMMON.PLUGIN_TITLE'} V$version", $WIKI, '', 'nojqm');
print LoxBerry::Log::get_notifications_html($lbpplugindir);
print "<style>\n" . LoxBerry::System::read_file("$lbptemplatedir/style.css") . "</style>\n";
print $pintemplate->output();
print qq{<div id="main_content" style="visibility:hidden">\n};
print $templateout->output();
print "</div>\n";
LoxBerry::Web::lbfooter();
exit;
