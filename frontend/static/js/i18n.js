/**
 * Lightweight citizen-facing i18n (Milestone 22).
 *
 * Deliberately NOT a framework -- a plain dictionary keyed by language
 * code, a small `t()` lookup, and a DOM-walking `applyTranslations()`
 * that reads `data-i18n`/`data-i18n-placeholder` attributes. Admin pages
 * never load this script and are unaffected -- admin stays English-only
 * per the milestone's explicit scope.
 *
 * Language persistence uses localStorage (the simplest existing
 * client-side mechanism available -- no server round-trip, no cookie).
 * Selecting a language never re-submits or alters any already-submitted
 * complaint; it only changes how this page's OWN static UI text and
 * status labels are displayed.
 */
(function () {
  const STORAGE_KEY = 'civicsync_language';
  const SUPPORTED_LANGUAGES = ['en', 'hi', 'bn'];
  const DEFAULT_LANGUAGE = 'en';

  const TRANSLATIONS = {
    en: {
      nav_report: 'Report Issue',
      nav_track: 'Track Status',
      nav_impact_map: 'Impact Map',
      nav_authority_portal: 'Authority Portal',
      lang_menu_heading: 'Language',
      lang_name_en: 'English',
      lang_name_hi: 'Hindi',
      lang_name_bn: 'Bengali',
      a11y_text_size: 'Text size',
      a11y_dark_mode: 'Dark mode',
      a11y_reduced_motion: 'Reduce motion',
      footer_tagline: 'AI-assisted civic issue reporting and accountability \u2014 from citizen report to authority resolution.',
      footer_heading_citizen: 'Citizen',
      footer_heading_authority: 'Authority',
      footer_copyright: '\u00a9 2026 CivicSync',

      hero_eyebrow: 'Your City, Your Impact',
      hero_subtitle: 'CivicSync turns your report into a structured civic record: classified by AI, routed to the right department, and trackable from submission through to resolution.',
      hero_capability_list_label: 'What CivicSync does',
      report_card_subtitle: "Describe the issue in your own words \u2014 CivicSync's AI will classify it and route it to the right department.",
      nav_impact_map_title: 'Coming in a future milestone',
      analysis_caption_complete: 'CivicSync\u2019s AI has classified your report.',
      analysis_heading_complete: 'Analysis Complete',
      analysis_category_label: 'Category',
      analysis_severity_label: 'Severity',
      analysis_department_label: 'AI-Suggested Department',
      analysis_disclaimer: "This is the AI's initial read \u2014 the department that officially picks up your report may differ after human review.",
      result_heading: 'Report Submitted',
      result_subtitle: 'Your report has been logged in CivicSync. You can track its progress at any time.',
      result_tracking_id_label: 'Public Tracking ID',
      result_department_label: 'Assigned Department',
      result_status_label: 'Status',
      theme_switch_to_light: 'Switch to light mode',
      theme_switch_to_dark: 'Switch to dark mode',
      authority_login_eyebrow: 'Internal Access',
      authority_login_subtitle: 'Sign in to access the CivicSync Command Center. This area is for authorized civic authority personnel only.',
      authority_login_username_label: 'Username',
      authority_login_password_label: 'Password',
      authority_login_show_password: 'Show password',
      authority_login_hide_password: 'Hide password',
      authority_login_submit: 'Sign In',
      authority_login_not_authority: 'Not an authority user?',
      authority_login_return_link: 'Return to the citizen site',
      hero_badge_ai: 'AI-Classified',
      hero_badge_languages: 'English · हिंदी · বাংলা',
      hero_badge_location: 'Location-Aware',
      hero_badge_tracking: 'Transparent Tracking',

      report_heading: 'Submit a Report',
      hero_heading_line1: 'Improve Your Community.',
      hero_heading_line2: 'Report with Precision.',
      how_it_works_heading: 'How it works',
      how_it_works_step1: '1. Describe the issue in plain language.',
      how_it_works_step2: '2. Gemini analyzes it \u2014 category, severity, and a suggested department.',
      how_it_works_step3: '3. You get a public tracking ID to follow it through to resolution.',
      departments_heading: 'Civic Departments',
      departments_intro: "Reports are routed to one of CivicSync's civic departments:",
      departments_loading: 'Loading\u2026',
      departments_unavailable: 'Department list unavailable right now.',
      department_name_electricity: 'Electricity',
      department_name_other: 'Other',
      department_name_parks_environment: 'Parks & Environment',
      department_name_public_health: 'Public Health',
      department_name_roads_transport: 'Roads & Transport',
      department_name_street_lighting: 'Street Lighting',
      department_name_waste_management: 'Waste Management',
      department_name_water_sanitation: 'Water & Sanitation',
      a11y_options: 'Accessibility options',
      a11y_decrease_text: 'Decrease text size',
      a11y_increase_text: 'Increase text size',
      a11y_reset_text: 'Reset text size',
      a11y_reset: 'Reset',
      nav_open_menu: 'Open menu',
      nav_close_menu: 'Close menu',
      report_subheading: "Describe the issue in your own words \u2014 CivicSync's AI will classify it and route it to the right department.",
      report_field_label: 'Describe the issue',
      report_field_placeholder: 'e.g. There has been no street light near our school for two weeks.',
      report_field_hint: "Include what's wrong, where, and how long it's been going on \u2014 the more detail you give, the better CivicSync's AI can classify and route it.",
      report_error_too_short: 'Please describe the issue in a bit more detail (at least 10 characters).',
      report_error_required: 'Please describe the issue before submitting.',
      report_submit_button: 'Submit Report',
      report_submitting_button: 'Submitting\u2026',

      location_label: 'Location of the problem',
      location_optional: '(optional)',
      location_use_button: 'Use my current location',
      location_locating_button: 'Locating\u2026',
      location_not_captured: 'Location not captured. This is optional and never blocks submission.',
      location_requesting: 'Requesting your location\u2026',
      location_captured: 'Location captured{accuracy}. You can adjust it below if needed.',
      location_try_again_button: 'Try Again',
      location_insecure_context: 'Location requires a secure (HTTPS) connection on this device. You can still submit your report without it.',
      location_permission_denied: "Your browser has this site blocked from using location \u2014 that's why no prompt appeared. Allow location for this site in your browser settings, then try again. You can still submit your report without it.",
      location_unavailable: 'Location is currently unavailable. You can still submit your report without it.',
      location_unsupported: 'Location is not supported by this browser. You can still submit your report.',
      location_coordinates_explainer: "These numbers mark the exact spot for this report (not shown on a map here) \u2014 they help the authority find the problem faster. You usually won't need to change them.",
      location_latitude: 'Latitude',
      location_longitude: 'Longitude',
      location_clear_button: 'Clear location',

      analysis_heading: 'AI Analysis in Progress',
      analysis_caption: "CivicSync sends your report to Gemini in a single request \u2014 these stages show what that analysis covers.",
      analysis_stage_analyzing: 'Analyzing complaint',
      analysis_stage_classifying: 'Classifying issue',
      analysis_stage_severity: 'Determining severity',
      analysis_stage_department: 'Identifying department',
      analysis_view_result_button: 'View Result',

      result_heading: 'Report Submitted',
      result_tracking_id_label: 'Tracking ID',
      result_department_label: 'Department',
      result_department_pending: 'Not yet assigned \u2014 pending review',
      result_status_label: 'Status',
      result_track_button: 'Track This Report',
      result_another_button: 'Report Another Issue',

      track_heading: 'Track a Report',
      track_subheading: 'Enter the public tracking ID you received when you submitted your report.',
      track_input_placeholder: 'e.g. CIV-A1B2C3D4E5F6',
      track_button: 'Track',
      track_not_found: 'No report was found with that tracking ID. Please check the ID and try again.',
      track_details_heading: 'Report Details',
      track_location_label: 'Location',
      track_duration_label: 'Duration',
      track_affected_population_label: 'Affected Population',
      track_severity_label: 'Severity',
      track_department_label: 'Assigned Department',
      track_department_pending: 'Not yet assigned \u2014 pending review',
      track_reported_label: 'Reported',
      track_updated_label: 'Last Updated',
      track_timeline_heading: 'Timeline',
      track_empty_message: 'Enter a public tracking ID above to see its status.',
      track_not_found_message: 'No report was found with the ID',
      track_not_found_hint: 'Double-check the ID from your submission confirmation and try again.',
      track_invalid_message: "That doesn't look like a valid CivicSync tracking ID.",
      track_invalid_hint_prefix: 'The expected format is',
      track_invalid_hint_suffix: 'followed by 12 uppercase letters/numbers, e.g.',
      track_error_default: 'Something went wrong.',
      track_id_prefix: 'Tracking ID:',
      reopen_intro_text: 'Submitting this sends a request to the authority for review \u2014 it does not reopen the issue immediately.',
      track_resolution_heading: 'Resolution',
      track_resolved_prefix: 'Resolved',
      track_evidence_heading: 'Resolution Evidence',
      track_not_specified: 'Not specified',

      reopen_prompt: 'Not satisfied with the resolution?',
      reopen_explainer: 'Submitting this sends a request to the authority for review \u2014 it does not reopen the issue immediately.',
      reopen_reason_placeholder: 'Explain why the resolution was inadequate or the issue has returned\u2026',
      reopen_submit_button: 'Request Reopening',
      reopen_pending_message: 'Your reopening request has been submitted and is pending authority review.',
      reopen_rejected_message: 'Your reopening request was not approved. You can submit a new request if the issue persists.',
      reopen_error_too_short: 'Please describe why the resolution was inadequate.',

      status_submitted: 'Submitted',
      status_classified: 'Classified',
      status_routed: 'Routed',
      status_acknowledged: 'Acknowledged',
      status_in_progress: 'In Progress',
      status_resolved: 'Resolved',
      status_closed: 'Closed',
      status_rejected: 'Rejected',
      status_reopened: 'Reopened',
    },

    hi: {
      nav_report: 'शिकायत दर्ज करें',
      nav_track: 'स्थिति देखें',
      nav_impact_map: 'प्रभाव मानचित्र',
      nav_authority_portal: 'प्राधिकरण पोर्टल',
      lang_menu_heading: 'भाषा',
      lang_name_en: 'अंग्रेज़ी',
      lang_name_hi: 'हिंदी',
      lang_name_bn: 'बांग्ला',
      a11y_text_size: 'टेक्स्ट का आकार',
      a11y_dark_mode: 'डार्क मोड',
      a11y_reduced_motion: 'गति कम करें',
      footer_tagline: 'AI-सहायता प्राप्त नागरिक शिकायत रिपोर्टिंग और जवाबदेही \u2014 नागरिक की रिपोर्ट से लेकर प्राधिकरण के समाधान तक।',
      footer_heading_citizen: 'नागरिक',
      footer_heading_authority: 'प्राधिकरण',
      footer_copyright: '\u00a9 2026 CivicSync',

      hero_eyebrow: 'आपका शहर, आपका प्रभाव',
      hero_subtitle: 'CivicSync आपकी शिकायत को एक संरचित नागरिक रिकॉर्ड में बदलता है: AI द्वारा वर्गीकृत, सही विभाग को भेजा गया, और सबमिशन से समाधान तक ट्रैक करने योग्य।',
      hero_capability_list_label: 'CivicSync क्या करता है',
      report_card_subtitle: 'समस्या को अपने शब्दों में बताएं \u2014 CivicSync का AI इसे वर्गीकृत करेगा और सही विभाग को भेजेगा।',
      nav_impact_map_title: 'भविष्य के माइलस्टोन में आ रहा है',
      analysis_caption_complete: 'CivicSync के AI ने आपकी शिकायत को वर्गीकृत कर दिया है।',
      analysis_heading_complete: 'विश्लेषण पूर्ण',
      analysis_category_label: 'श्रेणी',
      analysis_severity_label: 'गंभीरता',
      analysis_department_label: 'AI-अनुशंसित विभाग',
      analysis_disclaimer: 'यह AI की प्रारंभिक समझ है \u2014 जो विभाग आधिकारिक रूप से आपकी शिकायत लेगा वह मानव समीक्षा के बाद अलग हो सकता है।',
      result_heading: 'शिकायत सबमिट हो गई',
      result_subtitle: 'आपकी शिकायत CivicSync में दर्ज कर ली गई है। आप किसी भी समय इसकी प्रगति ट्रैक कर सकते हैं।',
      result_tracking_id_label: 'सार्वजनिक ट्रैकिंग आईडी',
      result_department_label: 'निर्धारित विभाग',
      result_status_label: 'स्थिति',
      theme_switch_to_light: 'लाइट मोड में बदलें',
      theme_switch_to_dark: 'डार्क मोड में बदलें',
      authority_login_eyebrow: 'आंतरिक पहुंच',
      authority_login_subtitle: 'CivicSync कमांड सेंटर तक पहुंचने के लिए साइन इन करें। यह क्षेत्र केवल अधिकृत नागरिक प्राधिकरण कर्मियों के लिए है।',
      authority_login_username_label: 'उपयोगकर्ता नाम',
      authority_login_password_label: 'पासवर्ड',
      authority_login_show_password: 'पासवर्ड दिखाएं',
      authority_login_hide_password: 'पासवर्ड छिपाएं',
      authority_login_submit: 'साइन इन करें',
      authority_login_not_authority: 'प्राधिकरण उपयोगकर्ता नहीं हैं?',
      authority_login_return_link: 'नागरिक साइट पर लौटें',
      hero_badge_ai: 'AI द्वारा वर्गीकृत',
      hero_badge_languages: 'English · हिंदी · বাংলা',
      hero_badge_location: 'स्थान-सहित',
      hero_badge_tracking: 'पारदर्शी ट्रैकिंग',

      report_heading: 'शिकायत दर्ज करें',
      hero_heading_line1: 'अपने समुदाय को बेहतर बनाएं।',
      hero_heading_line2: 'सटीकता के साथ शिकायत दर्ज करें।',
      how_it_works_heading: 'यह कैसे काम करता है',
      how_it_works_step1: '1. समस्या को सरल भाषा में बताएं।',
      how_it_works_step2: '2. Gemini इसका विश्लेषण करता है \u2014 श्रेणी, गंभीरता, और अनुशंसित विभाग।',
      how_it_works_step3: '3. आपको एक सार्वजनिक ट्रैकिंग आईडी मिलती है जिससे आप समाधान तक इसकी प्रगति देख सकते हैं।',
      departments_heading: 'नागरिक विभाग',
      departments_intro: 'शिकायतें CivicSync के नागरिक विभागों में से किसी एक को भेजी जाती हैं:',
      departments_loading: 'लोड हो रहा है\u2026',
      departments_unavailable: 'विभागों की सूची अभी उपलब्ध नहीं है।',
      department_name_electricity: 'विद्युत विभाग',
      department_name_other: 'अन्य',
      department_name_parks_environment: 'उद्यान एवं पर्यावरण',
      department_name_public_health: 'लोक स्वास्थ्य',
      department_name_roads_transport: 'सड़क एवं परिवहन',
      department_name_street_lighting: 'स्ट्रीट लाइटिंग',
      department_name_waste_management: 'अपशिष्ट प्रबंधन',
      department_name_water_sanitation: 'जल एवं स्वच्छता',
      a11y_options: 'सुलभता विकल्प',
      a11y_decrease_text: 'टेक्स्ट का आकार घटाएं',
      a11y_increase_text: 'टेक्स्ट का आकार बढ़ाएं',
      a11y_reset_text: 'टेक्स्ट का आकार रीसेट करें',
      a11y_reset: 'रीसेट',
      nav_open_menu: 'मेनू खोलें',
      nav_close_menu: 'मेनू बंद करें',
      report_subheading: 'समस्या को अपने शब्दों में बताएं \u2014 CivicSync का AI इसे वर्गीकृत करके सही विभाग को भेजेगा।',
      report_field_label: 'समस्या का विवरण दें',
      report_field_placeholder: 'उदाहरण: हमारे स्कूल के पास दो हफ्तों से स्ट्रीट लाइट खराब है।',
      report_field_hint: 'क्या गलत है, कहाँ है, और कब से चल रहा है \u2014 जितना विस्तार देंगे, AI उतना बेहतर वर्गीकृत कर पाएगा।',
      report_error_too_short: 'कृपया समस्या के बारे में थोड़ा और विस्तार से बताएं (कम से कम 10 अक्षर)।',
      report_error_required: 'कृपया सबमिट करने से पहले समस्या का विवरण दें।',
      report_submit_button: 'शिकायत सबमिट करें',
      report_submitting_button: 'सबमिट हो रहा है\u2026',

      location_label: 'समस्या का स्थान',
      location_optional: '(वैकल्पिक)',
      location_use_button: 'मेरा वर्तमान स्थान उपयोग करें',
      location_locating_button: 'स्थान खोजा जा रहा है\u2026',
      location_not_captured: 'स्थान दर्ज नहीं किया गया। यह वैकल्पिक है और सबमिट करने से नहीं रोकता।',
      location_requesting: 'आपका स्थान मांगा जा रहा है\u2026',
      location_captured: 'स्थान दर्ज हो गया{accuracy}। आवश्यकता होने पर आप इसे नीचे बदल सकते हैं।',
      location_try_again_button: 'फिर से कोशिश करें',
      location_insecure_context: 'स्थान सुविधा के लिए इस डिवाइस पर सुरक्षित (HTTPS) कनेक्शन आवश्यक है। आप बिना स्थान के भी शिकायत सबमिट कर सकते हैं।',
      location_permission_denied: 'आपके ब्राउज़र ने इस साइट के लिए स्थान की अनुमति पहले से ही अवरुद्ध कर रखी है \u2014 इसीलिए कोई अनुमति संदेश नहीं दिखा। अपनी ब्राउज़र सेटिंग्स में इस साइट के लिए स्थान की अनुमति दें, फिर फिर से कोशिश करें। आप बिना स्थान के भी शिकायत सबमिट कर सकते हैं।',
      location_unavailable: 'स्थान फिलहाल उपलब्ध नहीं है। आप बिना स्थान के भी शिकायत सबमिट कर सकते हैं।',
      location_unsupported: 'यह ब्राउज़र स्थान सुविधा का समर्थन नहीं करता। आप फिर भी शिकायत सबमिट कर सकते हैं।',
      location_coordinates_explainer: 'ये संख्याएं इस शिकायत का सटीक स्थान दर्शाती हैं (यहां मानचित्र पर नहीं दिखाया गया) \u2014 इससे प्राधिकरण को समस्या जल्दी ढूंढने में मदद मिलती है। आमतौर पर आपको इन्हें बदलने की आवश्यकता नहीं होगी।',
      location_latitude: 'अक्षांश',
      location_longitude: 'देशांतर',
      location_clear_button: 'स्थान हटाएं',

      analysis_heading: 'AI विश्लेषण जारी है',
      analysis_caption: 'CivicSync आपकी शिकायत को एक ही अनुरोध में Gemini को भेजता है \u2014 ये चरण उस विश्लेषण को दर्शाते हैं।',
      analysis_stage_analyzing: 'शिकायत का विश्लेषण हो रहा है',
      analysis_stage_classifying: 'समस्या वर्गीकृत हो रही है',
      analysis_stage_severity: 'गंभीरता निर्धारित हो रही है',
      analysis_stage_department: 'विभाग की पहचान हो रही है',
      analysis_view_result_button: 'परिणाम देखें',

      result_heading: 'शिकायत सबमिट हो गई',
      result_tracking_id_label: 'ट्रैकिंग आईडी',
      result_department_label: 'विभाग',
      result_department_pending: 'अभी तक असाइन नहीं \u2014 समीक्षा लंबित',
      result_status_label: 'स्थिति',
      result_track_button: 'इस शिकायत को ट्रैक करें',
      result_another_button: 'एक और शिकायत दर्ज करें',

      track_heading: 'शिकायत की स्थिति देखें',
      track_subheading: 'सबमिट करते समय मिली ट्रैकिंग आईडी दर्ज करें।',
      track_input_placeholder: 'उदाहरण: CIV-A1B2C3D4E5F6',
      track_button: 'ट्रैक करें',
      track_not_found: 'इस ट्रैकिंग आईडी से कोई शिकायत नहीं मिली। कृपया आईडी जांचें और पुनः प्रयास करें।',
      track_details_heading: 'शिकायत का विवरण',
      track_location_label: 'स्थान',
      track_duration_label: 'अवधि',
      track_affected_population_label: 'प्रभावित लोग',
      track_severity_label: 'गंभीरता',
      track_department_label: 'सौंपा गया विभाग',
      track_department_pending: 'अभी तक असाइन नहीं \u2014 समीक्षा लंबित',
      track_reported_label: 'दर्ज तिथि',
      track_updated_label: 'अंतिम अपडेट',
      track_timeline_heading: 'समयरेखा',
      track_empty_message: 'इसकी स्थिति देखने के लिए ऊपर एक सार्वजनिक ट्रैकिंग आईडी दर्ज करें।',
      track_not_found_message: 'इस आईडी से कोई रिपोर्ट नहीं मिली',
      track_not_found_hint: 'अपने सबमिशन पुष्टिकरण से आईडी दोबारा जांचें और फिर से प्रयास करें।',
      track_invalid_message: 'यह एक मान्य CivicSync ट्रैकिंग आईडी जैसा नहीं लगता।',
      track_invalid_hint_prefix: 'अपेक्षित प्रारूप है',
      track_invalid_hint_suffix: 'उसके बाद 12 बड़े अक्षर/अंक, उदाहरण के लिए',
      track_error_default: 'कुछ गलत हो गया।',
      track_id_prefix: 'ट्रैकिंग आईडी:',
      reopen_intro_text: 'इसे सबमिट करने से प्राधिकरण को समीक्षा हेतु एक अनुरोध भेजा जाता है \u2014 इससे शिकायत तुरंत दोबारा नहीं खुलती।',
      track_resolution_heading: 'समाधान',
      track_resolved_prefix: 'समाधान हुआ',
      track_evidence_heading: 'समाधान के प्रमाण',
      track_not_specified: 'निर्दिष्ट नहीं',

      reopen_prompt: 'समाधान से संतुष्ट नहीं हैं?',
      reopen_explainer: 'यह सबमिट करने से प्राधिकरण को समीक्षा हेतु अनुरोध भेजा जाता है \u2014 यह शिकायत को तुरंत फिर से नहीं खोलता।',
      reopen_reason_placeholder: 'बताएं कि समाधान अपर्याप्त क्यों था या समस्या फिर से क्यों हुई\u2026',
      reopen_submit_button: 'पुनः खोलने का अनुरोध करें',
      reopen_pending_message: 'आपका पुनः खोलने का अनुरोध सबमिट हो गया है और प्राधिकरण की समीक्षा लंबित है।',
      reopen_rejected_message: 'आपका पुनः खोलने का अनुरोध स्वीकृत नहीं किया गया। यदि समस्या बनी रहती है तो आप नया अनुरोध सबमिट कर सकते हैं।',
      reopen_error_too_short: 'कृपया बताएं कि समाधान अपर्याप्त क्यों था।',

      status_submitted: 'सबमिट हुई',
      status_classified: 'वर्गीकृत',
      status_routed: 'भेजी गई',
      status_acknowledged: 'स्वीकृत',
      status_in_progress: 'प्रगति में',
      status_resolved: 'समाधान हुआ',
      status_closed: 'बंद',
      status_rejected: 'अस्वीकृत',
      status_reopened: 'पुनः खोली गई',
    },

    bn: {
      nav_report: 'অভিযোগ জানান',
      nav_track: 'অবস্থা দেখুন',
      nav_impact_map: 'প্রভাব মানচিত্র',
      nav_authority_portal: 'কর্তৃপক্ষ পোর্টাল',
      lang_menu_heading: 'ভাষা',
      lang_name_en: 'ইংরেজি',
      lang_name_hi: 'হিন্দি',
      lang_name_bn: 'বাংলা',
      a11y_text_size: 'টেক্সট আকার',
      a11y_dark_mode: 'ডার্ক মোড',
      a11y_reduced_motion: 'গতি হ্রাস করুন',
      footer_tagline: 'AI-সহায়তাপ্রাপ্ত নাগরিক অভিযোগ প্রতিবেদন এবং জবাবদিহিতা \u2014 নাগরিকের প্রতিবেদন থেকে কর্তৃপক্ষের সমাধান পর্যন্ত।',
      footer_heading_citizen: 'নাগরিক',
      footer_heading_authority: 'কর্তৃপক্ষ',
      footer_copyright: '\u00a9 2026 CivicSync',

      hero_eyebrow: 'আপনার শহর, আপনার প্রভাব',
      hero_subtitle: 'CivicSync আপনার অভিযোগকে একটি কাঠামোবদ্ধ নাগরিক রেকর্ডে রূপান্তরিত করে: AI দ্বারা শ্রেণীবদ্ধ, সঠিক বিভাগে পাঠানো, এবং জমা থেকে সমাধান পর্যন্ত ট্র্যাকযোগ্য।',
      hero_capability_list_label: 'CivicSync কী করে',
      report_card_subtitle: 'সমস্যাটি নিজের ভাষায় বর্ণনা করুন \u2014 CivicSync-এর AI এটি শ্রেণীবদ্ধ করবে এবং সঠিক বিভাগে পাঠাবে।',
      nav_impact_map_title: 'ভবিষ্যতের মাইলস্টোনে আসছে',
      analysis_caption_complete: 'CivicSync-এর AI আপনার অভিযোগ শ্রেণীবদ্ধ করেছে।',
      analysis_heading_complete: 'বিশ্লেষণ সম্পূর্ণ',
      analysis_category_label: 'বিভাগ',
      analysis_severity_label: 'গুরুত্ব',
      analysis_department_label: 'AI-প্রস্তাবিত বিভাগ',
      analysis_disclaimer: 'এটি AI-এর প্রাথমিক পাঠ \u2014 যে বিভাগ আনুষ্ঠানিকভাবে আপনার অভিযোগ গ্রহণ করবে তা মানব পর্যালোচনার পরে ভিন্ন হতে পারে।',
      result_heading: 'অভিযোগ জমা হয়েছে',
      result_subtitle: 'আপনার অভিযোগ CivicSync-এ লগ করা হয়েছে। আপনি যেকোনো সময় এর অগ্রগতি ট্র্যাক করতে পারেন।',
      result_tracking_id_label: 'পাবলিক ট্র্যাকিং আইডি',
      result_department_label: 'নির্ধারিত বিভাগ',
      result_status_label: 'অবস্থা',
      theme_switch_to_light: 'লাইট মোডে পরিবর্তন করুন',
      theme_switch_to_dark: 'ডার্ক মোডে পরিবর্তন করুন',
      authority_login_eyebrow: 'অভ্যন্তরীণ প্রবেশাধিকার',
      authority_login_subtitle: 'CivicSync কমান্ড সেন্টার অ্যাক্সেস করতে সাইন ইন করুন। এই এলাকা শুধুমাত্র অনুমোদিত নাগরিক কর্তৃপক্ষের কর্মীদের জন্য।',
      authority_login_username_label: 'ব্যবহারকারীর নাম',
      authority_login_password_label: 'পাসওয়ার্ড',
      authority_login_show_password: 'পাসওয়ার্ড দেখান',
      authority_login_hide_password: 'পাসওয়ার্ড লুকান',
      authority_login_submit: 'সাইন ইন করুন',
      authority_login_not_authority: 'কর্তৃপক্ষের ব্যবহারকারী নন?',
      authority_login_return_link: 'নাগরিক সাইটে ফিরে যান',
      hero_badge_ai: 'AI দ্বারা শ্রেণীবদ্ধ',
      hero_badge_languages: 'English · হিন্দি · বাংলা',
      hero_badge_location: 'অবস্থান-সহ',
      hero_badge_tracking: 'স্বচ্ছ ট্র্যাকিং',

      report_heading: 'অভিযোগ জমা দিন',
      hero_heading_line1: 'আপনার সম্প্রদায়কে উন্নত করুন।',
      hero_heading_line2: 'নির্ভুলতার সাথে অভিযোগ জানান।',
      how_it_works_heading: 'এটি কীভাবে কাজ করে',
      how_it_works_step1: '১. সহজ ভাষায় সমস্যাটি বর্ণনা করুন।',
      how_it_works_step2: '২. Gemini এটি বিশ্লেষণ করে \u2014 বিভাগ, গুরুত্ব, এবং প্রস্তাবিত বিভাগ।',
      how_it_works_step3: '৩. আপনি একটি পাবলিক ট্র্যাকিং আইডি পাবেন যা দিয়ে সমাধান পর্যন্ত অগ্রগতি অনুসরণ করতে পারবেন।',
      departments_heading: 'নাগরিক বিভাগ',
      departments_intro: 'অভিযোগগুলি CivicSync-এর নাগরিক বিভাগগুলির একটিতে পাঠানো হয়:',
      departments_loading: 'লোড হচ্ছে\u2026',
      departments_unavailable: 'বিভাগের তালিকা এই মুহূর্তে উপলব্ধ নেই।',
      department_name_electricity: 'বিদ্যুৎ বিভাগ',
      department_name_other: 'অন্যান্য',
      department_name_parks_environment: 'উদ্যান ও পরিবেশ',
      department_name_public_health: 'জনস্বাস্থ্য',
      department_name_roads_transport: 'সড়ক ও পরিবহন',
      department_name_street_lighting: 'রাস্তার আলো',
      department_name_waste_management: 'বর্জ্য ব্যবস্থাপনা',
      department_name_water_sanitation: 'পানি ও স্যানিটেশন',
      a11y_options: 'অ্যাক্সেসযোগ্যতা বিকল্প',
      a11y_decrease_text: 'টেক্সট আকার কমান',
      a11y_increase_text: 'টেক্সট আকার বাড়ান',
      a11y_reset_text: 'টেক্সট আকার পুনরায় সেট করুন',
      a11y_reset: 'রিসেট',
      nav_open_menu: 'মেনু খুলুন',
      nav_close_menu: 'মেনু বন্ধ করুন',
      report_subheading: 'সমস্যাটি নিজের ভাষায় লিখুন \u2014 CivicSync-এর AI এটি শ্রেণীবদ্ধ করে সঠিক বিভাগে পাঠাবে।',
      report_field_label: 'সমস্যাটি বর্ণনা করুন',
      report_field_placeholder: 'উদাহরণ: আমাদের স্কুলের কাছে দুই সপ্তাহ ধরে রাস্তার বাতি নষ্ট।',
      report_field_hint: 'কী সমস্যা, কোথায়, এবং কতদিন ধরে চলছে তা লিখুন \u2014 যত বিস্তারিত দেবেন, AI তত ভালোভাবে শ্রেণীবদ্ধ করতে পারবে।',
      report_error_too_short: 'অনুগ্রহ করে সমস্যাটি আরেকটু বিস্তারিতভাবে লিখুন (কমপক্ষে ১০ অক্ষর)।',
      report_error_required: 'জমা দেওয়ার আগে অনুগ্রহ করে সমস্যাটি বর্ণনা করুন।',
      report_submit_button: 'অভিযোগ জমা দিন',
      report_submitting_button: 'জমা হচ্ছে\u2026',

      location_label: 'সমস্যার অবস্থান',
      location_optional: '(ঐচ্ছিক)',
      location_use_button: 'আমার বর্তমান অবস্থান ব্যবহার করুন',
      location_locating_button: 'অবস্থান খোঁজা হচ্ছে\u2026',
      location_not_captured: 'অবস্থান নেওয়া হয়নি। এটি ঐচ্ছিক এবং জমা দিতে বাধা দেয় না।',
      location_requesting: 'আপনার অবস্থান জানতে চাওয়া হচ্ছে\u2026',
      location_captured: 'অবস্থান নেওয়া হয়েছে{accuracy}। প্রয়োজনে নিচে পরিবর্তন করতে পারেন।',
      location_try_again_button: 'আবার চেষ্টা করুন',
      location_insecure_context: 'অবস্থান সুবিধার জন্য এই ডিভাইসে একটি নিরাপদ (HTTPS) সংযোগ প্রয়োজন। আপনি অবস্থান ছাড়াই আপনার অভিযোগ জমা দিতে পারেন।',
      location_permission_denied: 'আপনার ব্রাউজার ইতিমধ্যে এই সাইটের জন্য অবস্থান ব্যবহার অবরুদ্ধ করে রেখেছে — তাই কোনো অনুমতি বার্তা দেখা যায়নি। আপনার ব্রাউজার সেটিংসে এই সাইটের জন্য অবস্থান অনুমতি দিন, তারপর আবার চেষ্টা করুন। আপনি অবস্থান ছাড়াই আপনার অভিযোগ জমা দিতে পারেন।',
      location_unavailable: 'অবস্থান বর্তমানে অনুপলব্ধ। আপনি অবস্থান ছাড়াই অভিযোগ জমা দিতে পারেন।',
      location_unsupported: 'এই ব্রাউজার অবস্থান সুবিধা সমর্থন করে না। আপনি তবুও অভিযোগ জমা দিতে পারেন।',
      location_coordinates_explainer: 'এই সংখ্যাগুলি এই অভিযোগের সঠিক অবস্থান নির্দেশ করে (এখানে মানচিত্রে দেখানো হয়নি) \u2014 এটি কর্তৃপক্ষকে সমস্যাটি দ্রুত খুঁজে পেতে সাহায্য করে। সাধারণত আপনাকে এগুলি পরিবর্তন করার প্রয়োজন হবে না।',
      location_latitude: 'অক্ষাংশ',
      location_longitude: 'দ্রাঘিমাংশ',
      location_clear_button: 'অবস্থান মুছুন',

      analysis_heading: 'AI বিশ্লেষণ চলছে',
      analysis_caption: 'CivicSync আপনার অভিযোগ একটি অনুরোধে Gemini-কে পাঠায় \u2014 এই ধাপগুলি সেই বিশ্লেষণ দেখায়।',
      analysis_stage_analyzing: 'অভিযোগ বিশ্লেষণ করা হচ্ছে',
      analysis_stage_classifying: 'সমস্যা শ্রেণীবদ্ধ করা হচ্ছে',
      analysis_stage_severity: 'গুরুত্ব নির্ধারণ করা হচ্ছে',
      analysis_stage_department: 'বিভাগ শনাক্ত করা হচ্ছে',
      analysis_view_result_button: 'ফলাফল দেখুন',

      result_heading: 'অভিযোগ জমা হয়েছে',
      result_tracking_id_label: 'ট্র্যাকিং আইডি',
      result_department_label: 'বিভাগ',
      result_department_pending: 'এখনও নির্ধারিত হয়নি \u2014 পর্যালোচনা বাকি',
      result_status_label: 'অবস্থা',
      result_track_button: 'এই অভিযোগ ট্র্যাক করুন',
      result_another_button: 'আরেকটি অভিযোগ জানান',

      track_heading: 'অভিযোগের অবস্থা দেখুন',
      track_subheading: 'জমা দেওয়ার সময় পাওয়া ট্র্যাকিং আইডি লিখুন।',
      track_input_placeholder: 'উদাহরণ: CIV-A1B2C3D4E5F6',
      track_button: 'ট্র্যাক করুন',
      track_not_found: 'এই ট্র্যাকিং আইডি দিয়ে কোনো অভিযোগ পাওয়া যায়নি। আইডি যাচাই করে আবার চেষ্টা করুন।',
      track_details_heading: 'অভিযোগের বিবরণ',
      track_location_label: 'অবস্থান',
      track_duration_label: 'সময়কাল',
      track_affected_population_label: 'প্রভাবিত মানুষ',
      track_severity_label: 'গুরুত্ব',
      track_department_label: 'নির্ধারিত বিভাগ',
      track_department_pending: 'এখনও নির্ধারিত হয়নি \u2014 পর্যালোচনা বাকি',
      track_reported_label: 'জমার তারিখ',
      track_updated_label: 'সর্বশেষ আপডেট',
      track_timeline_heading: 'সময়রেখা',
      track_empty_message: 'এর অবস্থা দেখতে উপরে একটি পাবলিক ট্র্যাকিং আইডি লিখুন।',
      track_not_found_message: 'এই আইডি দিয়ে কোনো প্রতিবেদন পাওয়া যায়নি',
      track_not_found_hint: 'আপনার জমার নিশ্চিতকরণ থেকে আইডিটি আবার পরীক্ষা করুন এবং আবার চেষ্টা করুন।',
      track_invalid_message: 'এটি একটি বৈধ CivicSync ট্র্যাকিং আইডি বলে মনে হচ্ছে না।',
      track_invalid_hint_prefix: 'প্রত্যাশিত ফরম্যাট হলো',
      track_invalid_hint_suffix: 'তারপরে 12টি বড় হাতের অক্ষর/সংখ্যা, যেমন',
      track_error_default: 'কিছু ভুল হয়েছে।',
      track_id_prefix: 'ট্র্যাকিং আইডি:',
      reopen_intro_text: 'এটি জমা দিলে কর্তৃপক্ষের কাছে পর্যালোচনার জন্য একটি অনুরোধ পাঠানো হয় \u2014 এটি সাথে সাথে অভিযোগটি পুনরায় খোলে না।',
      track_resolution_heading: 'সমাধান',
      track_resolved_prefix: 'সমাধান হয়েছে',
      track_evidence_heading: 'সমাধানের প্রমাণ',
      track_not_specified: 'উল্লেখ নেই',

      reopen_prompt: 'সমাধানে সন্তুষ্ট নন?',
      reopen_explainer: 'এটি জমা দিলে কর্তৃপক্ষের কাছে পর্যালোচনার অনুরোধ যায় \u2014 এটি সাথে সাথে অভিযোগ পুনরায় খোলে না।',
      reopen_reason_placeholder: 'সমাধান কেন যথেষ্ট ছিল না বা সমস্যা কেন ফিরে এসেছে তা লিখুন\u2026',
      reopen_submit_button: 'পুনরায় খোলার অনুরোধ করুন',
      reopen_pending_message: 'আপনার পুনরায় খোলার অনুরোধ জমা হয়েছে এবং কর্তৃপক্ষের পর্যালোচনা বাকি আছে।',
      reopen_rejected_message: 'আপনার পুনরায় খোলার অনুরোধ অনুমোদিত হয়নি। সমস্যা বজায় থাকলে আপনি নতুন অনুরোধ জমা দিতে পারেন।',
      reopen_error_too_short: 'অনুগ্রহ করে লিখুন সমাধান কেন যথেষ্ট ছিল না।',

      status_submitted: 'জমা হয়েছে',
      status_classified: 'শ্রেণীবদ্ধ',
      status_routed: 'প্রেরিত',
      status_acknowledged: 'স্বীকৃত',
      status_in_progress: 'চলমান',
      status_resolved: 'সমাধান হয়েছে',
      status_closed: 'বন্ধ',
      status_rejected: 'প্রত্যাখ্যাত',
      status_reopened: 'পুনরায় খোলা হয়েছে',
    },
  };

  function getLanguage() {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored && SUPPORTED_LANGUAGES.includes(stored)) return stored;
    } catch (e) {
      // localStorage unavailable (e.g. private browsing) -- fall back
      // to the default rather than failing the page.
    }
    return DEFAULT_LANGUAGE;
  }

  function setLanguage(lang) {
    if (!SUPPORTED_LANGUAGES.includes(lang)) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, lang);
    } catch (e) {
      // Ignore storage failures -- the language still applies for this
      // page view via the in-memory dictionary.
    }
    applyTranslations();
    document.dispatchEvent(new CustomEvent('civicsync:languagechange', { detail: { language: lang } }));
  }

  function t(key, vars) {
    const lang = getLanguage();
    const dict = TRANSLATIONS[lang] || TRANSLATIONS[DEFAULT_LANGUAGE];
    let value = dict[key] !== undefined ? dict[key] : TRANSLATIONS[DEFAULT_LANGUAGE][key];
    if (value === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach((k) => {
        value = value.replace(`{${k}}`, vars[k]);
      });
    }
    return value;
  }

  /** Translates every element carrying data-i18n / data-i18n-placeholder
   * in the current document. Safe to call repeatedly (e.g. after a
   * language switch, or after dynamically-inserted content). */
  function applyTranslations() {
    document.documentElement.setAttribute('lang', getLanguage());
    document.querySelectorAll('[data-i18n]').forEach((el) => {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
    });
    document.querySelectorAll('[data-i18n-aria-label]').forEach((el) => {
      el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria-label')));
    });
    document.querySelectorAll('[data-language-option]').forEach((el) => {
      el.classList.toggle('is-active', el.getAttribute('data-language-option') === getLanguage());
    });
  }

  function statusLabel(statusValue) {
    const key = `status_${String(statusValue || '').toLowerCase()}`;
    return t(key) !== key ? t(key) : statusValue;
  }

  window.CivicSyncI18n = {
    SUPPORTED_LANGUAGES,
    getLanguage,
    setLanguage,
    t,
    applyTranslations,
    statusLabel,
  };

  document.addEventListener('DOMContentLoaded', () => {
    applyTranslations();
    document.querySelectorAll('[data-language-option]').forEach((el) => {
      el.addEventListener('click', () => setLanguage(el.getAttribute('data-language-option')));
    });
  });
})();
