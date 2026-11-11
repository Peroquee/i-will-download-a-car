*** Settings ***
Resource    resources/common.resource
Suite Setup       Open The App And Sign In As Admin
Suite Teardown    Close All Browsers
Test Setup        Open Dashboard Page

*** Test Cases ***
The Overview Shows The Three States And The Alert Summary
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:status-internet-state    Online
    Element Should Contain    id:status-vpn-state    VPN running
    Element Should Contain    id:status-watchdog-state    Watching
    Element Should Contain    id:dash-alert-counts    Errors
    Element Should Contain    id:dash-alert-counts    unread
    Element Should Not Be Visible    id:dash-vpn

The Jobs Tab Shows Active And Finished Downloads
    Click Element    id:dash-tab-jobs
    Wait Until Element Is Visible    id:dash-jobs-view
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:dash-recent-list    Seeder - Seeded Video
    Element Should Contain    id:dash-recent-list    Seeded Magnet (2000)

Finished Downloads Start At Ten And Expand To Fifty
    Click Element    id:dash-tab-jobs
    Wait Until Element Is Visible    id:dash-jobs-view
    Wait Until Keyword Succeeds    15s    0.5s    Finished Rows Equal    10
    Click Button    id:dash-limit
    Wait Until Keyword Succeeds    15s    0.5s    Finished Rows At Least    12
    Click Button    id:dash-limit
    Wait Until Keyword Succeeds    15s    0.5s    Finished Rows Equal    10

A Failed Magnet Job Can Be Retried
    Click Element    id:dash-tab-jobs
    Wait Until Element Is Visible    id:dash-jobs-view
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'Seeded Magnet (2000)')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Try again']
    Click Button    ${row}//button[normalize-space(.)='Try again']
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:dash-active-list    Seeded Magnet (2000)
    ${recent}=    Get Text    id:dash-recent-list
    Should Not Contain    ${recent}    Seeded Magnet (2000)

A Failed Job Can Be Dismissed And Restored
    Click Element    id:dash-tab-jobs
    Wait Until Element Is Visible    id:dash-jobs-view
    ${row}=    Set Variable    xpath=//li[contains(@class,'job')][contains(.,'Seeder - Seeded Video')]
    Wait Until Element Is Visible    ${row}//button[normalize-space(.)='Dismiss']
    Click Button    ${row}//button[normalize-space(.)='Dismiss']
    Wait Until Keyword Succeeds    20s    0.5s    Page Should Not Contain    Seeder - Seeded Video
    Click Button    id:dash-show-dismissed
    Wait Until Element Is Visible    ${row}
    Element Should Contain    ${row}    Show again
    Click Button    ${row}//button[normalize-space(.)='Show again']
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    ${row}    Dismiss

An Alert Can Be Marked As Read On The Alert Tab
    Click Element    id:dash-tab-alerts
    Wait Until Element Is Visible    id:dash-alerts-view
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Be Visible    xpath=(//li[contains(@class,'alert-item')]//button[normalize-space(.)='Mark as read'])[1]
    ${count}=    Get Element Count    xpath=//li[contains(@class,'alert-item')]//button[normalize-space(.)='Mark as read']
    Should Be True    ${count} >= 1
    Click Button    xpath=(//li[contains(@class,'alert-item')]//button[normalize-space(.)='Mark as read'])[1]
    Wait Until Keyword Succeeds    10s    0.5s    Alert Buttons Decrease    ${count}

The Alert Strip Marks Everything As Read
    Wait Until Element Is Visible    id:alert-strip-ack
    Click Button    id:alert-strip-ack
    Wait Until Element Is Not Visible    id:alert-strip

The Overview Shows The Storage Bar And Its Legend
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:dash-storage-legend    Free:
    Element Should Contain    id:dash-storage-legend    Movie
    Element Should Be Visible    id:dash-storage-bar

The Vpn Tab Shows The State And History
    Click Element    id:dash-tab-vpn
    Wait Until Element Is Visible    id:dash-vpn
    Wait Until Keyword Succeeds    15s    0.5s    Element Should Contain    id:dash-vpn-now    VPN running
    Element Should Contain    id:dash-vpn-list    VPN running
    Element Should Not Be Visible    id:dash-storage
    Element Should Not Be Visible    id:dash-deploy


*** Keywords ***
Finished Rows Equal
    [Arguments]    ${expected}
    ${rows}=    Get Element Count    xpath=//*[@id='dash-recent-list']/li
    Should Be Equal As Integers    ${rows}    ${expected}

Finished Rows At Least
    [Arguments]    ${expected}
    ${rows}=    Get Element Count    xpath=//*[@id='dash-recent-list']/li
    Should Be True    ${rows} >= ${expected}
