:- use_module(library(clpfd)).

:- dynamic file_concept/2.
:- dynamic file_import/2.
:- dynamic file_project/2.
:- dynamic file_modified_days/2.
:- dynamic preference_fact/3.
:- dynamic task_project/2.
:- dynamic research_source/4.
:- dynamic research_claim/3.

schedule_tasks(Durations, Earliest, Latest, Busy, Starts, Ends) :-
    same_length(Durations, Starts),
    same_length(Durations, Ends),
    constrain_tasks(Durations, Earliest, Latest, Starts, Ends),
    serialized(Starts, Durations),
    maplist(avoid_all_busy(Busy), Starts, Ends),
    labeling([ffc, up], Starts).

constrain_task(Duration, Earliest, Latest, Start, End) :-
    Start #>= Earliest,
    End #= Start + Duration,
    End #=< Latest.

constrain_tasks([], [], [], [], []).
constrain_tasks([Duration|Durations], [First|Earliest], [Last|Latest], [Start|Starts], [End|Ends]) :-
    constrain_task(Duration, First, Last, Start, End),
    constrain_tasks(Durations, Earliest, Latest, Starts, Ends).

avoid_all_busy(Busy, Start, End) :-
    maplist(avoid_busy(Start, End), Busy).

avoid_busy(Start, End, busy(BusyStart, BusyEnd)) :-
    End #=< BusyStart #\/ Start #>= BusyEnd.

interval_conflict(StartA, EndA, StartB, EndB) :-
    StartA #< EndB,
    StartB #< EndA.

dependency_order(StartBefore, DurationBefore, StartAfter) :-
    StartAfter #>= StartBefore + DurationBefore.

related_file(File, Related, imported_by) :-
    file_import(Related, File),
    File \= Related.
related_file(File, Related, imports) :-
    file_import(File, Related),
    File \= Related.
related_file(File, Related, shared_concept) :-
    file_concept(File, Concept),
    file_concept(Related, Concept),
    File \= Related.
related_file(File, Related, same_project) :-
    file_project(File, Project),
    file_project(Related, Project),
    File \= Related.

file_change_impact(File, Affected, direct_import) :-
    file_import(Affected, File).
file_change_impact(File, Affected, transitive_import) :-
    file_import(Intermediate, File),
    file_import(Affected, Intermediate),
    Affected \= File.

stale_file(File) :-
    file_modified_days(File, Days),
    Days #>= 180.

preference_influences(Task, preferred_project, Value) :-
    task_project(Task, Value),
    preference_fact(preferred_project, Value, Confidence),
    Confidence #>= 75.
preference_influences(_Task, preferred_period, Value) :-
    preference_fact(preferred_period, Value, Confidence),
    Confidence #>= 75.
preference_influences(_Task, preferred_duration, Value) :-
    preference_fact(preferred_duration, Value, Confidence),
    Confidence #>= 75.

source_quality(Source, Score) :-
    research_source(Source, Rank, AgeDays, DomainCount),
    Freshness #= max(0, 100 - min(100, AgeDays)),
    Diversity #= min(30, DomainCount * 10),
    Position #= max(0, 40 - Rank * 4),
    Score #= Freshness + Diversity + Position.

corroborated_source(Source, Other, Claim) :-
    research_claim(Source, Claim, Value),
    research_claim(Other, Claim, Value),
    Source \= Other.

conflicting_source(Source, Other, Claim) :-
    research_claim(Source, Claim, Value),
    research_claim(Other, Claim, OtherValue),
    Source \= Other,
    Value \= OtherValue.

evidence_score(Source, Score) :-
    source_quality(Source, Base),
    findall(Other, corroborated_source(Source, Other, _), Corroborated0),
    sort(Corroborated0, Corroborated), length(Corroborated, SupportCount),
    findall(Other, conflicting_source(Source, Other, _), Conflicted0),
    sort(Conflicted0, Conflicted), length(Conflicted, ConflictCount),
    Score #= Base + SupportCount * 15 - ConflictCount * 20.
