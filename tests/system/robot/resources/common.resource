*** Settings ***
Library    SeleniumLibrary    timeout=15s    run_on_failure=Capture Page Screenshot

*** Variables ***
${BASE_URL}          http://127.0.0.1:8000
${BROWSER}           chrome
${CHROME_BINARY}     ${EMPTY}
${CHROMEDRIVER}      ${EMPTY}
${ADMIN_USER}        boss
${ADMIN_PASSWORD}    a long test password

*** Keywords ***
Open The App
    ${options}=    Catenate    SEPARATOR=;
    ...    add_argument("--headless=new")
    ...    add_argument("--no-sandbox")
    ...    add_argument("--disable-dev-shm-usage")
    ...    add_argument("--disable-gpu")
    ...    add_argument("--lang=en-US")
    ...    add_argument("--window-size=1280,900")
    IF    $CHROME_BINARY
        ${options}=    Catenate    SEPARATOR=;    ${options}    binary_location="${CHROME_BINARY}"
    END
    Open Browser    ${BASE_URL}    ${BROWSER}    options=${options}    executable_path=${CHROMEDRIVER}
    Set Selenium Timeout    15s
    Set Screenshot Directory    ${OUTPUT DIR}
    Register Keyword To Run On Failure    Capture Page Screenshot

Open The App And Sign In As Admin
    Open The App
    Go To Login
    Sign In As Admin

Go To Login
    Go To    ${BASE_URL}/
    Wait Until Element Is Visible    id:login-form
    Wait Until Element Is Not Visible    id:js-warning

Use English
    Wait Until Element Is Visible    id:nav-input
    ${label}=    Get Text    id:nav-input
    IF    "${label}" == "Eingabe"
        Click Element    id:lang
        Wait Until Keyword Succeeds    5s    0.2s    Element Should Contain    id:nav-input    Input
    END

Sign In As
    [Arguments]    ${name}    ${password}
    Go To Login
    Input Text    id:login-user    ${name}
    Input Password    id:login-password    ${password}
    Click Button    id:login-submit
    Wait Until Element Is Visible    id:form
    Wait Until Element Is Not Visible    id:js-warning
    Use English

Sign In As Admin
    Sign In As    ${ADMIN_USER}    ${ADMIN_PASSWORD}

Ensure Signed In As Admin
    Wait Until Element Is Not Visible    id:js-warning
    ${signed_in}=    Run Keyword And Return Status    Element Should Be Visible    id:form
    IF    not ${signed_in}
        Sign In As Admin
    ELSE
        Use English
    END

Open Input Page
    Go To    ${BASE_URL}/
    Ensure Signed In As Admin
    Wait Until Element Is Visible    id:form

Log Out
    Click Element    id:account-open
    Wait Until Element Is Visible    id:account
    Click Button    id:account-logout
    Wait Until Element Is Visible    id:login-form

Select Mode
    [Arguments]    ${mode}
    Click Element    css:label[for=mode-${mode}]
    Wait Until Element Is Visible    id:${mode}-fields

Submit The Form
    Scroll Element Into View    id:submit
    Click Button    id:submit

Wait For Job Result
    Wait Until Keyword Succeeds    20s    0.5s    Element Should Contain    id:result    Job #

Assert Field Error
    [Arguments]    ${error_id}    ${expected}
    Wait Until Element Is Visible    id:${error_id}
    Element Should Contain    id:${error_id}    ${expected}

Assert Field Invalid
    [Arguments]    ${field_id}
    Element Attribute Value Should Be    id:${field_id}    aria-invalid    true

Assert Field Valid
    [Arguments]    ${field_id}
    ${value}=    Get Element Attribute    id:${field_id}    aria-invalid
    Should Not Be Equal    ${value}    true

Assert Page Headline
    [Arguments]    ${element_id}    ${text}
    Wait Until Element Is Visible    id:${element_id}
    Element Should Contain    id:${element_id}    ${text}

Language Differs
    [Arguments]    ${before}
    ${now}=    Get Text    id:nav-input
    Should Not Be Equal    ${now}    ${before}
