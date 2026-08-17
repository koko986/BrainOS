query_important_task(Task) :-
    important_task(Task).

query_high_priority(Task) :-
    high_priority(Task).

query_high_priority_reason(Task, Reason) :-
    high_priority_reason(Task, Reason).

