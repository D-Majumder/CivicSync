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
      footer_tagline: 'AI-assisted civic accountability platform',
      footer_authority_portal: 'Authority Portal',

      report_heading: 'Submit a Report',
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
      location_permission_denied: 'Location permission was denied. You can still submit your report without it.',
      location_unavailable: 'Location is currently unavailable. You can still submit your report without it.',
      location_unsupported: 'Location is not supported by this browser. You can still submit your report.',
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
      track_resolution_heading: 'Resolution',
      track_resolved_prefix: 'Resolved',
      track_evidence_heading: 'Resolution Evidence',
      track_not_specified: 'Not specified',

      reopen_prompt: 'Not satisfied with the resolution?',
      reopen_explainer: 'Submitting this sends a request to the authority for review \u2014 it does not reopen the issue immediately.',
      reopen_reason_placeholder: 'Explain why the resolution was inadequate or the issue has returned\u2026',
      reopen_submit_button: 'Request Reopening',
      reopen_pending_message: 'Your reopening request has been submitted and is pending authority review.',
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
      footer_tagline: 'AI-सहायता प्राप्त नागरिक जवाबदेही मंच',
      footer_authority_portal: 'प्राधिकरण पोर्टल',

      report_heading: 'शिकायत दर्ज करें',
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
      location_permission_denied: 'स्थान की अनुमति अस्वीकार कर दी गई। आप बिना स्थान के भी शिकायत सबमिट कर सकते हैं।',
      location_unavailable: 'स्थान फिलहाल उपलब्ध नहीं है। आप बिना स्थान के भी शिकायत सबमिट कर सकते हैं।',
      location_unsupported: 'यह ब्राउज़र स्थान सुविधा का समर्थन नहीं करता। आप फिर भी शिकायत सबमिट कर सकते हैं।',
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
      track_resolution_heading: 'समाधान',
      track_resolved_prefix: 'समाधान हुआ',
      track_evidence_heading: 'समाधान के प्रमाण',
      track_not_specified: 'निर्दिष्ट नहीं',

      reopen_prompt: 'समाधान से संतुष्ट नहीं हैं?',
      reopen_explainer: 'यह सबमिट करने से प्राधिकरण को समीक्षा हेतु अनुरोध भेजा जाता है \u2014 यह शिकायत को तुरंत फिर से नहीं खोलता।',
      reopen_reason_placeholder: 'बताएं कि समाधान अपर्याप्त क्यों था या समस्या फिर से क्यों हुई\u2026',
      reopen_submit_button: 'पुनः खोलने का अनुरोध करें',
      reopen_pending_message: 'आपका पुनः खोलने का अनुरोध सबमिट हो गया है और प्राधिकरण की समीक्षा लंबित है।',
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
      footer_tagline: 'AI-সহায়ক নাগরিক জবাবদিহিতা প্ল্যাটফর্ম',
      footer_authority_portal: 'কর্তৃপক্ষ পোর্টাল',

      report_heading: 'অভিযোগ জমা দিন',
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
      location_permission_denied: 'অবস্থানের অনুমতি প্রত্যাখ্যান করা হয়েছে। আপনি অবস্থান ছাড়াই অভিযোগ জমা দিতে পারেন।',
      location_unavailable: 'অবস্থান বর্তমানে অনুপলব্ধ। আপনি অবস্থান ছাড়াই অভিযোগ জমা দিতে পারেন।',
      location_unsupported: 'এই ব্রাউজার অবস্থান সুবিধা সমর্থন করে না। আপনি তবুও অভিযোগ জমা দিতে পারেন।',
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
      track_resolution_heading: 'সমাধান',
      track_resolved_prefix: 'সমাধান হয়েছে',
      track_evidence_heading: 'সমাধানের প্রমাণ',
      track_not_specified: 'উল্লেখ নেই',

      reopen_prompt: 'সমাধানে সন্তুষ্ট নন?',
      reopen_explainer: 'এটি জমা দিলে কর্তৃপক্ষের কাছে পর্যালোচনার অনুরোধ যায় \u2014 এটি সাথে সাথে অভিযোগ পুনরায় খোলে না।',
      reopen_reason_placeholder: 'সমাধান কেন যথেষ্ট ছিল না বা সমস্যা কেন ফিরে এসেছে তা লিখুন\u2026',
      reopen_submit_button: 'পুনরায় খোলার অনুরোধ করুন',
      reopen_pending_message: 'আপনার পুনরায় খোলার অনুরোধ জমা হয়েছে এবং কর্তৃপক্ষের পর্যালোচনা বাকি আছে।',
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
