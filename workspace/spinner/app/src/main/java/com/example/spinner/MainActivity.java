package com.example.spinner;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.widget.*;
import android.view.View;

public class MainActivity extends AppCompatActivity {

    String branch[] = {"cse","ece","mec","civ","bme"};
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);

        TextView tv = findViewById(R.id.tv);
        Spinner sp = findViewById(R.id.sp);

        ArrayAdapter<String> adapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,branch);

        sp.setAdapter(adapter);

        sp.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener(){
            public void onItemSelected(AdapterView<?> parent,View view, int pos, long id){
                tv.setText(branch[pos]);
            }
            public void onNothingSelected(AdapterView<?> parent){

            }
        });

    }
}