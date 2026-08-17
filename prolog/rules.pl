active_project(Project) :-
    project(Project),
    active(Project).

important_task(Task) :-
    task(Task),
    belongs_to(Task, Project),
    active_project(Project).

high_priority(Task) :-
    important_task(Task),
    deadline_soon(Task).

high_priority(Task) :-
    important_task(Task),
    depends_on(Task, _Dependency).

high_priority_reason(Task, belongs_to_active_project) :-
    important_task(Task).

high_priority_reason(Task, deadline_soon) :-
    high_priority(Task),
    deadline_soon(Task).

high_priority_reason(Task, important_task) :-
    high_priority(Task),
    important_task(Task).

high_priority_reason(Task, dependency) :-
    high_priority(Task),
    depends_on(Task, _Dependency).

