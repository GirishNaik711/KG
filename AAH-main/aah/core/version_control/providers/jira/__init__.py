"""Jira provider adapter — placeholder.

To add Jira support: implement provider.py (TrackerProvider subclass), mapper.py
(WorkItem <-> Jira fields), and client.py (Jira REST wrapper), then call
``register("jira", JiraProvider)`` at the bottom of provider.py and import this
package from providers/__init__.py. No core code changes are required.
"""
