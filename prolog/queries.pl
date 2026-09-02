query_important_task(Task) :-
    important_task(Task).

query_high_priority(Task) :-
    high_priority(Task).

query_high_priority_reason(Task, Reason) :-
    high_priority_reason(Task, Reason).

query_blocked_task(Task) :-
    blocked_task(Task).

query_overdue_task(Task) :-
    overdue_task(Task).

query_dependency_chain(Task, Dependency) :-
    dependency_chain(Task, Dependency).

query_current_project_focus(Project) :-
    current_project_focus(Project).

query_morning_priority(Task) :-
    morning_priority(Task).

query_morning_priority_reason(Task, Reason) :-
    morning_priority_reason(Task, Reason).
